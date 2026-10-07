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
    database_url: str = "sqlite://"            # in-memory: for a quick local start and tests
    jwt_secret: str = "dev-only-change-me-dev-only-change-me-0123456789"
    jwt_issuer: str = "codecheck"              # Kong's jwt plugin matches this `iss` to a credential
    access_ttl_s: int = 15 * 60                # NFR-8: access 15 minutes
    refresh_ttl_s: int = 7 * 24 * 3600         # NFR-8: refresh 7 days
    kafka_bootstrap: str = ""                  # empty = no outbox relay (tests, local runs)
    internal_token: str = ""                   # empty = /internal/* is closed
    teacher_invite_code: str = "teacher-invite"  # empty = nobody can self-register as a teacher
    admin_email: str = ""
    admin_password: str = ""
    topic_assignment_updated: str = "assignment.updated"

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            database_url=_str("DATABASE_URL", cls.database_url),
            jwt_secret=_str("JWT_SECRET", cls.jwt_secret),
            jwt_issuer=_str("JWT_ISSUER", cls.jwt_issuer),
            access_ttl_s=_int("ACCESS_TTL_SECONDS", cls.access_ttl_s),
            refresh_ttl_s=_int("REFRESH_TTL_SECONDS", cls.refresh_ttl_s),
            kafka_bootstrap=os.environ.get("KAFKA_BOOTSTRAP", "").strip(),
            internal_token=os.environ.get("INTERNAL_TOKEN", "").strip(),
            teacher_invite_code=os.environ.get("TEACHER_INVITE_CODE", cls.teacher_invite_code).strip(),
            admin_email=os.environ.get("ADMIN_EMAIL", "").strip().lower(),
            admin_password=os.environ.get("ADMIN_PASSWORD", ""),
        )
