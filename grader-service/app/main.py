"""grader-service has no public API (Kong has no route to it). Only operational endpoints:
liveness, readiness and Prometheus metrics. The real work happens in Kafka consumer threads."""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from . import metrics
from .config import Settings
from .runtime import Runtime


@asynccontextmanager
async def lifespan(app: FastAPI):
    runtime = Runtime(Settings.from_env())
    runtime.start()
    app.state.runtime = runtime
    try:
        yield
    finally:
        runtime.stop()


app = FastAPI(title="grader-service", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.get("/health/live")
def live() -> dict:
    return {"status": "ok"}


@app.get("/health/ready")
def ready(request: Request) -> JSONResponse:
    ok = request.app.state.runtime.ready()
    return JSONResponse({"status": "ready" if ok else "starting"}, status_code=200 if ok else 503)


@app.get("/metrics")
def prometheus() -> Response:
    body, content_type = metrics.render()
    return Response(content=body, media_type=content_type)
