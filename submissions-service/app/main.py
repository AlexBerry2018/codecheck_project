"""HTTP layer of submissions-service (FastAPI). Run with `uvicorn app.main:create_app --factory`.

Public routes (all behind Kong, see kong/kong.yml):
  POST /api/submissions                                   send a solution            -> 202
  GET  /api/submissions/{id}                              status and per-test result
  GET  /api/submissions?assignment_id=&user_id=           attempt history
  POST /api/submissions/assignments/{id}/regrade          teacher: re-check everything
  GET  /api/leaderboard/{assignment_id}                   top of the assignment
Operational: /health/live, /health/ready, /metrics (not routed by Kong).
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from contextlib import asynccontextmanager
from typing import Optional

import jwt
from fastapi import Depends, FastAPI, File, Form, Header, Query, Request, Response, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import metrics, models  # noqa: F401  (models must be imported before create_all)
from .cache import Cache
from .config import Settings
from .courses_client import CoursesClient
from .db import Base, make_engine
from .logging_setup import correlation_id_var, setup_logging
from .security import User, decode_access_token
from .service import Invalid, ServiceError, SubmissionService, TooLarge, Unauthorized
from .sources import MongoSources

log = logging.getLogger("submissions")
QUIET_PATHS = ("/health/", "/metrics")


def _wait_for_schema(engine, attempts: int = 30) -> None:
    """create_all with retries: the database container may still be starting."""
    for attempt in range(1, attempts + 1):
        try:
            Base.metadata.create_all(engine)
            return
        except Exception as exc:                           # noqa: BLE001
            if attempt == attempts:
                raise
            log.warning("database is not ready (%s), retrying in 2 s", exc)
            time.sleep(2)


def _error(status: int, code: str, message: str, headers: Optional[dict] = None,
           details: Optional[list] = None) -> JSONResponse:
    body: dict = {"error": {"code": code, "message": message}}
    if details:
        body["error"]["details"] = details
    return JSONResponse(body, status_code=status, headers=headers)


def current_user(request: Request) -> User:
    scheme, _, token = request.headers.get("Authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise Unauthorized("send `Authorization: Bearer <access token>`",
                           {"WWW-Authenticate": "Bearer"})
    settings: Settings = request.app.state.settings
    try:
        return decode_access_token(settings.jwt_secret, settings.jwt_issuer, token.strip())
    except jwt.PyJWTError as exc:
        raise Unauthorized("the access token is invalid or expired", {"WWW-Authenticate": "Bearer"}) from exc


def create_app(settings: Optional[Settings] = None, *, cache=None, sources=None, courses=None,
               start_workers: bool = True) -> FastAPI:
    """Adapters can be injected (tests pass MemoryCache, MemorySources and a fake courses client)."""
    settings = settings or Settings.from_env()
    setup_logging("submissions-service")

    engine = make_engine(settings.database_url)
    _wait_for_schema(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    cache = cache if cache is not None else Cache(settings.valkey_url)
    sources = sources if sources is not None else MongoSources(settings.mongo_url, settings.mongo_db,
                                                               settings.source_ttl_days)
    courses = courses if courses is not None else CoursesClient(
        settings.courses_url, settings.internal_token, cache, settings.assignment_cache_ttl_s)
    service = SubmissionService(settings, factory, cache, sources, courses)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        stop = threading.Event()
        threads: list[threading.Thread] = []
        publisher = None
        if start_workers and settings.kafka_bootstrap:
            from .kafka_io import ConsumerLoop, Publisher        # lazy: tests have no Kafka client
            from .outbox import OutboxRelay

            def lag(group: str, topic: str, partition: int, value: int) -> None:
                metrics.CONSUMER_LAG.labels(group, topic, str(partition)).set(value)

            publisher = Publisher(settings.kafka_bootstrap, "submissions-service")
            threads = [
                OutboxRelay(factory, publisher, stop),
                ConsumerLoop("submissions-graded", settings.kafka_bootstrap, "submissions",
                             [settings.topic_graded], service.handle_graded, stop, on_lag=lag,
                             poison_counter=metrics.POISON),
            ]
            for thread in threads:
                thread.start()
            try:
                if hasattr(sources, "ensure_index"):
                    sources.ensure_index()
            except Exception:                                    # noqa: BLE001
                log.exception("could not create the TTL index now, will retry on first write")
        app.state.threads = threads
        try:
            yield
        finally:
            stop.set()
            for thread in threads:
                thread.join(timeout=10)
            for closer in (publisher, sources, courses):
                try:
                    if closer is not None:
                        closer.close()
                except Exception:                                # noqa: BLE001
                    pass

    app = FastAPI(title="submissions-service", version="1.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.service = service
    app.state.session_factory = factory
    app.state.cache = cache
    app.state.sources = sources
    app.state.threads = []

    # ------------------------------------------------------------------ cross-cutting
    @app.middleware("http")
    async def observe(request: Request, call_next):
        cid = request.headers.get("X-Request-ID") or uuid.uuid4().hex
        token = correlation_id_var.set(cid)
        started = time.perf_counter()
        try:
            response = await call_next(request)
        finally:
            correlation_id_var.reset(token)
        route = getattr(request.scope.get("route"), "path", "unmatched")
        metrics.REQUESTS.labels(request.method, route, str(response.status_code)).inc()
        response.headers["X-Request-ID"] = cid
        if not request.url.path.startswith(QUIET_PATHS):
            log.info("request", extra={"ctx": {
                "method": request.method, "path": request.url.path, "status": response.status_code,
                "duration_ms": round((time.perf_counter() - started) * 1000, 1), "cid": cid}})
        return response

    @app.exception_handler(ServiceError)
    async def service_error(_request: Request, exc: ServiceError) -> JSONResponse:
        return _error(exc.status, exc.code, exc.message, exc.headers)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
        details = [f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()]
        return _error(422, "validation", "invalid input", details=details)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
        return _error(exc.status_code, "not_found" if exc.status_code == 404 else "http_error",
                      str(exc.detail), getattr(exc, "headers", None))

    @app.exception_handler(Exception)
    async def unexpected(_request: Request, exc: Exception) -> JSONResponse:
        log.error("unhandled error", exc_info=exc)
        return _error(500, "internal_error", "unexpected error")

    # ------------------------------------------------------------------ operations
    @app.get("/health/live")
    def live() -> dict:
        return {"status": "ok"}

    @app.get("/health/ready")
    def ready(request: Request) -> JSONResponse:
        try:
            with factory() as db:
                db.execute(text("select 1"))
        except Exception:                                        # noqa: BLE001
            return _error(503, "not_ready", "database is unavailable")
        dead = [t.name for t in request.app.state.threads if not t.is_alive()]
        if dead:
            return _error(503, "not_ready", f"background threads stopped: {', '.join(dead)}")
        return JSONResponse({"status": "ok"})

    @app.get("/metrics")
    def prometheus() -> Response:
        body, content_type = metrics.render()
        return Response(content=body, headers={"Content-Type": content_type})

    # ------------------------------------------------------------------ submissions
    @app.post("/api/submissions", status_code=202)
    def create_submission(request: Request,
                          assignment_id: int = Form(...),
                          file: Optional[UploadFile] = File(None),
                          code: Optional[str] = Form(None),
                          idempotency_key: Optional[str] = Header(None),
                          user: User = Depends(current_user)) -> JSONResponse:
        """Multipart form: `assignment_id` plus either a `file` or the source in `code`."""
        limit = settings.max_code_bytes
        if (file is None) == (code is None):
            raise Invalid("send exactly one of `file` or `code`")
        if file is not None:
            raw = file.file.read(limit + 1)                      # never read more than the limit
            if len(raw) > limit:
                raise TooLarge(f"the solution is larger than {limit} bytes")
            try:
                source = raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise Invalid("the file must be UTF-8 text") from exc
        else:
            source = code or ""
        view, replay = service.submit(user, assignment_id, source, idempotency_key,
                                      correlation_id_var.get())
        headers = {"Location": f"/api/submissions/{view['id']}"}
        if replay:
            headers["Idempotent-Replay"] = "true"
        return JSONResponse(view, status_code=202, headers=headers)

    @app.get("/api/submissions/{submission_id}")
    def get_submission(submission_id: int, user: User = Depends(current_user)) -> dict:
        return service.get(user, submission_id)

    @app.get("/api/submissions")
    def list_submissions(assignment_id: Optional[int] = None, user_id: Optional[int] = None,
                         limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0),
                         user: User = Depends(current_user)) -> list:
        return service.history(user, assignment_id, user_id, limit, offset)

    @app.post("/api/submissions/assignments/{assignment_id}/regrade")
    def regrade(assignment_id: int, user: User = Depends(current_user)) -> dict:
        return service.regrade(user, assignment_id, correlation_id_var.get())

    @app.get("/api/leaderboard/{assignment_id}")
    def leaderboard(assignment_id: int, limit: int = Query(20, ge=1, le=100),
                    user: User = Depends(current_user)) -> dict:
        return service.leaderboard(user, assignment_id, limit)

    return app
