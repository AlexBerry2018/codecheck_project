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
    database_url: str = "sqlite://"            # in-memory: for tests and a quick local start
    valkey_url: str = "redis://valkey:6379/0"
    mongo_url: str = "mongodb://mongo:27017"
    mongo_db: str = "sources"
    courses_url: str = "http://courses-service:8000"
    internal_token: str = ""
    jwt_secret: str = "dev-only-change-me-dev-only-change-me-0123456789"
    jwt_issuer: str = "codecheck"
    kafka_bootstrap: str = ""                  # empty = no relay, no consumer (tests)

    topic_created: str = "submission.created"
    topic_graded: str = "submission.graded"

    max_code_bytes: int = 64 * 1024            # FR-4 / design doc: above this the answer is 413
    user_limit_per_min: int = 10               # NFR-9: exact per-user limit (Kong's is per token)
    assignment_cache_ttl_s: int = 60           # `assignment:{id}`
    status_ttl_pending_s: int = 3              # `status:{id}` while queued
    status_ttl_final_s: int = 600              # `status:{id}` once graded
    idem_ttl_s: int = 24 * 3600                # `idem:{user}:{key}`
    source_ttl_days: int = 365                 # NFR-11

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            database_url=_str("DATABASE_URL", cls.database_url),
            valkey_url=_str("VALKEY_URL", cls.valkey_url),
            mongo_url=_str("MONGO_URL", cls.mongo_url),
            mongo_db=_str("MONGO_DB", cls.mongo_db),
            courses_url=_str("COURSES_URL", cls.courses_url),
            internal_token=os.environ.get("INTERNAL_TOKEN", "").strip(),
            jwt_secret=_str("JWT_SECRET", cls.jwt_secret),
            jwt_issuer=_str("JWT_ISSUER", cls.jwt_issuer),
            kafka_bootstrap=os.environ.get("KAFKA_BOOTSTRAP", "").strip(),
            max_code_bytes=_int("MAX_CODE_BYTES", cls.max_code_bytes),
            user_limit_per_min=_int("USER_LIMIT_PER_MIN", cls.user_limit_per_min),
            assignment_cache_ttl_s=_int("ASSIGNMENT_CACHE_TTL_S", cls.assignment_cache_ttl_s),
        )
