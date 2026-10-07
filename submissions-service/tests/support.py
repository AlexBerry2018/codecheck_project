"""Shared helpers. Imported by the test modules after their importorskip checks."""
from __future__ import annotations

import time

import jwt

from app.config import Settings
from app.courses_client import CoursesUnavailable
from app.events import dumps, make_event

SECRET = "test-secret-test-secret-test-secret-0123"

CARD = {"id": 1, "assignment_id": 1, "course_id": 1, "owner_id": 10, "title": "Sum", "is_open": True,
        "deadline": None, "time_limit_ms": 2000, "memory_limit_mb": 128, "tests_version": 1}


def token(user_id: int, role: str = "student", **overrides) -> dict:
    """Authorization header with a valid access token (claims can be overridden)."""
    claims = {"iss": "codecheck", "sub": str(user_id), "role": role, "type": "access",
              "exp": int(time.time()) + 600}
    claims.update(overrides)
    return {"Authorization": "Bearer " + jwt.encode(claims, SECRET, algorithm="HS256")}


class FakeCourses:
    """Stands in for CoursesClient: cards can be edited, the 'service' can be switched off."""

    def __init__(self) -> None:
        self.cards = {1: dict(CARD)}
        self.down = False

    def get_assignment(self, assignment_id: int, fresh: bool = False):
        if self.down:
            raise CoursesUnavailable("courses-service is down")
        card = self.cards.get(assignment_id)
        return dict(card) if card else None

    def close(self) -> None:
        pass


def settings(**overrides) -> Settings:
    return Settings(**{"database_url": "sqlite://", "jwt_secret": SECRET, "kafka_bootstrap": "",
                       **overrides})


def make_app(**overrides):
    from app.cache import MemoryCache
    from app.main import create_app
    from app.sources import MemorySources

    courses = FakeCourses()
    app = create_app(settings(**overrides), cache=MemoryCache(), sources=MemorySources(), courses=courses)
    app.state.courses = courses
    return app


def graded_bytes(submission_id: int, *, version: int = 1, verdict: str = "accepted", passed: int = 2,
                 total: int = 2, **extra) -> bytes:
    """A `submission.graded` message exactly as grader-service publishes it."""
    payload = {"submission_id": submission_id, "assignment_id": 1, "user_id": 1,
               "tests_version": version, "verdict": verdict, "tests_passed": passed,
               "tests_total": total, "time_ms": 12,
               "tests": [{"index": 0, "verdict": "accepted", "time_ms": 5, "hidden": False}],
               "graded_at": "2026-10-07T10:00:00+00:00"}
    payload.update(extra)
    return dumps(make_event("submission.graded", payload, "cid-1"))
