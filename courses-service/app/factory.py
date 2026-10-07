from __future__ import annotations

import logging
import threading
import time
import uuid
from typing import Optional

from flask import Flask, g, request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from werkzeug.exceptions import HTTPException
from werkzeug.security import generate_password_hash

from . import metrics, models  # noqa: F401  (models must be imported before create_all)
from .config import Settings
from .db import Base, make_engine
from .logging_setup import correlation_id_var, setup_logging
from .models import User
from .routes import bp
from .security import error_response

log = logging.getLogger("courses")
QUIET_PATHS = ("/health/", "/metrics")


def _wait_for_schema(engine, attempts: int = 30) -> None:
    """create_all with retries: the database container may still be starting."""
    for attempt in range(1, attempts + 1):
        try:
            Base.metadata.create_all(engine)
            return
        except Exception as exc:                         # noqa: BLE001
            if attempt == attempts:
                raise
            log.warning("database is not ready (%s), retrying in 2 s", exc)
            time.sleep(2)


def _seed_admin(factory, settings: Settings) -> None:
    if not settings.admin_email or not settings.admin_password:
        return
    with factory() as db:
        if db.scalar(select(User).where(User.email == settings.admin_email)) is None:
            db.add(User(email=settings.admin_email, role="admin",
                        password_hash=generate_password_hash(settings.admin_password)))
            try:
                db.commit()
            except IntegrityError:                       # another replica created it a moment ago
                db.rollback()
                return
            log.info("admin account %s created", settings.admin_email)


def create_app(settings: Optional[Settings] = None, start_relay: bool = True) -> Flask:
    settings = settings or Settings.from_env()
    setup_logging("courses-service")
    app = Flask(__name__)
    app.json.ensure_ascii = False
    app.config["SETTINGS"] = settings

    engine = make_engine(settings.database_url)
    _wait_for_schema(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    app.extensions["session_factory"] = factory
    _seed_admin(factory, settings)

    @app.before_request
    def open_request():
        g.db = factory()
        g.started = time.perf_counter()
        g.cid = request.headers.get("X-Request-ID") or uuid.uuid4().hex
        correlation_id_var.set(g.cid)

    @app.after_request
    def close_request(response):
        route = request.url_rule.rule if request.url_rule else "unmatched"
        metrics.REQUESTS.labels(request.method, route, str(response.status_code)).inc()
        if not request.path.startswith(QUIET_PATHS):
            log.info("request", extra={"ctx": {
                "method": request.method, "path": request.path, "status": response.status_code,
                "duration_ms": round((time.perf_counter() - g.get("started", time.perf_counter())) * 1000, 1)}})
        return response

    @app.teardown_request
    def release_db(exc):
        db = g.pop("db", None)
        if db is not None:
            if exc is not None:
                db.rollback()
            db.close()

    @app.errorhandler(HTTPException)
    def http_error(exc: HTTPException):
        return error_response(exc.code or 500, (exc.name or "error").lower().replace(" ", "_"),
                              exc.description or exc.name)

    @app.errorhandler(Exception)
    def unexpected(exc: Exception):
        log.exception("unhandled error")
        return error_response(500, "internal_error", "unexpected error")

    app.register_blueprint(bp)

    if start_relay and settings.kafka_bootstrap:
        from .kafka_io import Publisher                 # imported lazily: tests have no Kafka client
        from .outbox import OutboxRelay
        stop = threading.Event()
        app.extensions["relay_stop"] = stop
        OutboxRelay(factory, Publisher(settings.kafka_bootstrap, "courses-service"), stop).start()
    return app
