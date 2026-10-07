"""Prometheus metrics (NFR-10). Falls back to no-ops if prometheus_client is missing, so
the business logic can be imported and tested without it."""
from __future__ import annotations

try:
    from prometheus_client import CONTENT_TYPE_LATEST, Counter, generate_latest
except ImportError:                                    # pragma: no cover
    CONTENT_TYPE_LATEST = "text/plain"

    class _Noop:
        def labels(self, *args, **kwargs):
            return self

        def inc(self, *args, **kwargs):
            pass

    def Counter(*args, **kwargs):                      # noqa: N802
        return _Noop()

    def generate_latest():
        return b""

REQUESTS = Counter("courses_http_requests_total", "HTTP requests", ["method", "route", "status"])
OUTBOX_PUBLISHED = Counter("courses_outbox_published_total", "Outbox rows delivered to Kafka")
OUTBOX_FAILURES = Counter("courses_outbox_failures_total", "Outbox relay failures")


def render() -> tuple[bytes, str]:
    return generate_latest(), CONTENT_TYPE_LATEST
