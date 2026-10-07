"""Tables owned by submissions-service (database `submissions_db`). Ids of users and
assignments are plain integers: they belong to courses-service, there are no cross-database keys."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import JSON, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base

QUEUED = "queued"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(value: Optional[datetime]) -> Optional[datetime]:
    """SQLite hands back naive datetimes, PostgreSQL aware ones: normalise to aware UTC."""
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


class Submission(Base):
    __tablename__ = "submissions"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer)
    assignment_id: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32), default=QUEUED)        # queued | <verdict>
    verdict: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    idempotency_key: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    tests_version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        UniqueConstraint("user_id", "idempotency_key", name="uq_submission_idempotency"),
        Index("ix_submissions_user_assignment", "user_id", "assignment_id", "created_at"),
        Index("ix_submissions_assignment", "assignment_id"),
    )


class Result(Base):
    """One verdict per (submission, tests_version): a duplicate event hits the primary key
    and is ignored, which makes the consumer idempotent."""
    __tablename__ = "results"

    submission_id: Mapped[int] = mapped_column(ForeignKey("submissions.id", ondelete="CASCADE"),
                                               primary_key=True)
    tests_version: Mapped[int] = mapped_column(Integer, primary_key=True)
    verdict: Mapped[str] = mapped_column(String(32))
    tests_passed: Mapped[int] = mapped_column(Integer, default=0)
    tests_total: Mapped[int] = mapped_column(Integer, default=0)
    time_ms: Mapped[int] = mapped_column(Integer, default=0)
    details: Mapped[list] = mapped_column(JSON, default=list)             # per-test results
    graded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (Index("ix_results_verdict", "verdict"),)


class Outbox(Base):
    """Transactional outbox, same pattern as in courses-service."""
    __tablename__ = "outbox"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    topic: Mapped[str] = mapped_column(String(128))
    key: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (Index("ix_outbox_sent_at", "sent_at"),)
