"""Prometheus metrics (NFR-10). Falls back to no-ops if prometheus_client is missing, so
the business logic can be imported and tested without it."""
from __future__ import annotations

try:
    from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, generate_latest
except ImportError:                                    # pragma: no cover
    CONTENT_TYPE_LATEST = "text/plain"

    class _Noop:
        def labels(self, *args, **kwargs):
            return self

        def inc(self, *args, **kwargs):
            pass

        def set(self, *args, **kwargs):
            pass

    def Counter(*args, **kwargs):                      # noqa: N802
        return _Noop()

    Gauge = Counter

    def generate_latest():
        return b""

REQUESTS = Counter("submissions_http_requests_total", "HTTP requests", ["method", "route", "status"])
CREATED = Counter("submissions_created_total", "Accepted submissions")
REPLAYS = Counter("submissions_idempotent_replays_total", "Requests answered from an Idempotency-Key")
RATE_LIMITED = Counter("submissions_rate_limited_total", "Submissions refused by the per-user limit")
GRADED = Counter("submissions_graded_events_total", "submission.graded events applied", ["verdict"])
POISON = Counter("submissions_poison_messages_total", "Messages skipped because they are malformed")
OUTBOX_PUBLISHED = Counter("submissions_outbox_published_total", "Outbox rows delivered to Kafka")
OUTBOX_FAILURES = Counter("submissions_outbox_failures_total", "Outbox relay failures")
CONSUMER_LAG = Gauge("kafka_consumer_lag", "Messages behind the end of a partition",
                     ["group", "topic", "partition"])


def render() -> tuple[bytes, str]:
    return generate_latest(), CONTENT_TYPE_LATEST
