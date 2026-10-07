"""Runtime settings, read once from environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name, "")
    return int(raw) if raw.strip() else default


def _str(name: str, default: str) -> str:
    raw = os.environ.get(name, "")
    return raw if raw.strip() else default


@dataclass(frozen=True)
class Settings:
    kafka_bootstrap: str = "kafka:9092"
    valkey_url: str = "redis://valkey:6379/0"
    mongo_url: str = "mongodb://mongo:27017"
    mongo_db: str = "run_logs"
    courses_url: str = "http://courses-service:8000"
    internal_token: str = ""

    # "docker": one throw-away container per test run (default, used in compose)
    # "local":  plain subprocess with ulimit. NOT isolated, for development and tests only.
    sandbox_mode: str = "docker"
    sandbox_image: str = "python:3.12-alpine"
    max_parallel: int = 2          # concurrent sandbox runs inside one grader replica

    max_attempts: int = 3          # attempts before a message goes to the DLQ (design doc: 3)
    retry_delay_s: int = 5         # delay grows linearly: attempt * retry_delay_s
    tests_cache_ttl_s: int = 3600  # `tests:{assignment}:{version}` TTL, 1 hour
    run_logs_ttl_days: int = 90    # TTL index on run_logs, NFR-11

    topic_created: str = "submission.created"
    topic_retry: str = "submission.created.retry"
    topic_dlq: str = "submission.created.dlq"
    topic_graded: str = "submission.graded"
    topic_assignment_updated: str = "assignment.updated"

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            kafka_bootstrap=_str("KAFKA_BOOTSTRAP", cls.kafka_bootstrap),
            valkey_url=_str("VALKEY_URL", cls.valkey_url),
            mongo_url=_str("MONGO_URL", cls.mongo_url),
            mongo_db=_str("MONGO_DB", cls.mongo_db),
            courses_url=_str("COURSES_URL", cls.courses_url),
            internal_token=_str("INTERNAL_TOKEN", ""),
            sandbox_mode=_str("SANDBOX_MODE", cls.sandbox_mode),
            sandbox_image=_str("SANDBOX_IMAGE", cls.sandbox_image),
            max_parallel=_int("MAX_PARALLEL", cls.max_parallel),
            max_attempts=_int("MAX_ATTEMPTS", cls.max_attempts),
            retry_delay_s=_int("RETRY_DELAY_S", cls.retry_delay_s),
            tests_cache_ttl_s=_int("TESTS_CACHE_TTL_S", cls.tests_cache_ttl_s),
            run_logs_ttl_days=_int("RUN_LOGS_TTL_DAYS", cls.run_logs_ttl_days),
        )
