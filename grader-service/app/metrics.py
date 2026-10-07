"""Prometheus metrics (NFR-10). Falls back to no-ops if prometheus_client is missing, so
the business logic can be imported and tested without it."""
from __future__ import annotations

try:
    from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
    ENABLED = True
except ImportError:                                    # pragma: no cover
    ENABLED = False
    CONTENT_TYPE_LATEST = "text/plain"

    class _Noop:
        def labels(self, *args, **kwargs):
            return self

        def inc(self, *args, **kwargs):
            pass

        def set(self, *args, **kwargs):
            pass

        def observe(self, *args, **kwargs):
            pass

    def Counter(*args, **kwargs):                      # noqa: N802
        return _Noop()

    Gauge = Histogram = Counter

    def generate_latest():
        return b""

GRADED = Counter("grader_submissions_total", "Graded submissions by verdict", ["verdict"])
GRADE_SECONDS = Histogram("grader_grading_seconds", "Time to grade one submission",
                          buckets=(0.5, 1, 2, 5, 10, 20, 30, 60, 120))
RETRIES = Counter("grader_retries_total", "Messages sent to the retry topic")
DLQ = Counter("grader_dlq_total", "Messages sent to the dead letter topic")
POISON = Counter("grader_poison_messages_total", "Messages skipped because they are malformed")
CONSUMER_LAG = Gauge("kafka_consumer_lag", "Messages behind the end of a partition",
                     ["group", "topic", "partition"])


def render() -> tuple[bytes, str]:
    return generate_latest(), CONTENT_TYPE_LATEST
