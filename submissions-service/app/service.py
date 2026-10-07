"""Business logic of submissions-service. No FastAPI in here: the routes in main.py only translate
HTTP into these calls, so everything is testable with SQLite + MemoryCache + MemorySources.

Flow (UC-03, UC-04, UC-05 of the design doc):
  submit()        write the submission and its `submission.created` event in ONE transaction
                  (outbox), answer 202; the relay thread publishes the event afterwards.
  handle_graded() consume `submission.graded`: idempotent upsert of the result, refresh caches.
  regrade()       re-publish `submission.created` for submissions graded under older tests.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from . import metrics
from .config import Settings
from .courses_client import CoursesUnavailable
from .events import PoisonMessage, make_event, parse_event
from .logging_setup import correlation_id_var
from .models import QUEUED, Outbox, Result, Submission, as_utc, utcnow
from .ratelimit import check_user_rate
from .security import User

log = logging.getLogger("submissions.service")

REGRADE_LOCK_S = 60            # a second regrade of the same assignment inside this window is a 409
REGRADE_BATCH = 200
LEADERBOARD_DEFAULT = 20


# ---------------------------------------------------------------------------------------- errors
class ServiceError(Exception):
    """An expected failure with an HTTP meaning; main.py turns it into the JSON error body."""
    status = 400
    code = "bad_request"

    def __init__(self, message: str, headers: Optional[dict[str, str]] = None) -> None:
        super().__init__(message)
        self.message = message
        self.headers = headers or {}


class Unauthorized(ServiceError):
    status, code = 401, "unauthorized"


class Forbidden(ServiceError):
    status, code = 403, "forbidden"


class NotFound(ServiceError):
    status, code = 404, "not_found"


class Conflict(ServiceError):
    status, code = 409, "conflict"


class AssignmentClosed(Conflict):
    code = "assignment_closed"


class TooLarge(ServiceError):
    status, code = 413, "payload_too_large"


class Invalid(ServiceError):
    status, code = 422, "validation"


class RateLimited(ServiceError):
    status, code = 429, "rate_limited"


class Unavailable(ServiceError):
    status, code = 503, "unavailable"


# ---------------------------------------------------------------------------------------- views
def score_of(passed: int, total: int) -> int:
    """Leaderboard score: percentage of passed tests."""
    return round(100 * passed / total) if total > 0 else 0


def _iso(value: Optional[datetime]) -> Optional[str]:
    value = as_utc(value)
    return value.isoformat() if value else None


def submission_view(sub: Submission, result: Optional[Result] = None) -> dict[str, Any]:
    view: dict[str, Any] = {
        "id": sub.id, "assignment_id": sub.assignment_id, "user_id": sub.user_id,
        "status": sub.status, "verdict": sub.verdict, "tests_version": sub.tests_version,
        "created_at": _iso(sub.created_at),
    }
    if result is not None:
        view.update(tests_passed=result.tests_passed, tests_total=result.tests_total,
                    time_ms=result.time_ms, graded_at=_iso(result.graded_at),
                    tests=result.details or [])
    return view


# ---------------------------------------------------------------------------------------- service
class SubmissionService:
    def __init__(self, settings: Settings, session_factory: "sessionmaker[Session]", cache, sources,
                 courses) -> None:
        self.settings = settings
        self.session_factory = session_factory
        self.cache = cache
        self.sources = sources
        self.courses = courses

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _status_key(submission_id: int) -> str:
        return f"status:{submission_id}"

    @staticmethod
    def _leaderboard_key(assignment_id: int) -> str:
        return f"lb:{assignment_id}"

    @staticmethod
    def _idem_key(user_id: int, key: str) -> str:
        return f"idem:{user_id}:{key}"

    def _card(self, assignment_id: int, fresh: bool = False) -> dict:
        try:
            card = self.courses.get_assignment(assignment_id, fresh=fresh)
        except CoursesUnavailable as exc:
            raise Unavailable("courses-service is unavailable") from exc
        if card is None:
            raise NotFound("no such assignment")
        return card

    @staticmethod
    def _ensure_open(card: dict) -> None:
        if not card.get("is_open", True):
            raise AssignmentClosed("the assignment is closed")
        deadline = card.get("deadline")
        if deadline:
            try:
                due = as_utc(datetime.fromisoformat(deadline))
            except ValueError:
                due = None
            if due is not None and utcnow() > due:
                raise AssignmentClosed("the deadline has passed")

    @staticmethod
    def _clean_key(raw: Optional[str]) -> Optional[str]:
        key = (raw or "").strip()
        if not key:
            return None
        if len(key) > 128:
            raise Invalid("Idempotency-Key is longer than 128 characters")
        return key

    def _created_event(self, sub: Submission, code: str, correlation_id: str,
                       regrade: bool = False) -> Outbox:
        payload: dict[str, Any] = {"submission_id": sub.id, "assignment_id": sub.assignment_id,
                                   "user_id": sub.user_id, "tests_version": sub.tests_version,
                                   "code": code}
        if regrade:
            payload["regrade"] = True
        return Outbox(topic=self.settings.topic_created, key=str(sub.id),
                      payload=make_event("submission.created", payload, correlation_id))

    # ------------------------------------------------------------------ UC-03: submit
    def submit(self, user: User, assignment_id: int, code: str, idempotency_key: Optional[str],
               correlation_id: str = "-") -> tuple[dict[str, Any], bool]:
        """Returns (view, replay). `replay` is True when the Idempotency-Key was seen before."""
        s = self.settings
        if not code.strip():
            raise Invalid("the solution is empty")
        if len(code.encode("utf-8")) > s.max_code_bytes:
            raise TooLarge(f"the solution is larger than {s.max_code_bytes} bytes")
        key = self._clean_key(idempotency_key)

        if key:                                   # a retry must not burn the user's quota
            replay = self._replay(user.id, assignment_id, key)
            if replay is not None:
                return replay, True

        wait = check_user_rate(self.cache, user.id, s.user_limit_per_min)
        if wait is not None:
            metrics.RATE_LIMITED.inc()
            raise RateLimited(f"at most {s.user_limit_per_min} submissions per minute",
                              {"Retry-After": str(wait)})

        card = self._card(assignment_id)
        self._ensure_open(card)

        with self.session_factory() as db:
            sub = Submission(user_id=user.id, assignment_id=assignment_id, status=QUEUED,
                             idempotency_key=key, tests_version=int(card["tests_version"]))
            db.add(sub)
            try:
                db.flush()                        # id + the unique (user, idempotency_key) check
            except IntegrityError:
                db.rollback()
                existing = None
                if key:
                    existing = db.scalar(select(Submission).where(
                        Submission.user_id == user.id, Submission.idempotency_key == key))
                if existing is None:
                    raise
                metrics.REPLAYS.inc()
                return submission_view(existing), True
            try:
                self.sources.put(sub.id, code)    # MongoDB first: a failure rolls everything back
            except Exception as exc:              # noqa: BLE001 - any storage failure is a 503
                db.rollback()
                log.exception("could not store the source of a submission")
                raise Unavailable("source storage is unavailable") from exc
            db.add(self._created_event(sub, code, correlation_id))
            db.commit()                           # submission + outbox row, atomically
            view = submission_view(sub)

        if key:
            self.cache.set_json(self._idem_key(user.id, key), {"id": view["id"]}, s.idem_ttl_s)
        metrics.CREATED.inc()
        return view, False

    def _replay(self, user_id: int, assignment_id: int, key: str) -> Optional[dict[str, Any]]:
        cached = self.cache.get_json(self._idem_key(user_id, key))
        with self.session_factory() as db:
            sub = None
            if isinstance(cached, dict) and isinstance(cached.get("id"), int):
                sub = db.get(Submission, cached["id"])
                if sub is not None and (sub.user_id != user_id or sub.idempotency_key != key):
                    sub = None
            if sub is None:                       # the cache is only a shortcut, the table is the truth
                sub = db.scalar(select(Submission).where(Submission.user_id == user_id,
                                                         Submission.idempotency_key == key))
            if sub is None:
                return None
            if sub.assignment_id != assignment_id:
                raise Conflict("this Idempotency-Key was already used for another assignment")
            metrics.REPLAYS.inc()
            return submission_view(sub)

    # ------------------------------------------------------------------ UC-04: read
    def get(self, user: User, submission_id: int) -> dict[str, Any]:
        s = self.settings
        view = self.cache.get_json(self._status_key(submission_id))
        if view is None:
            with self.session_factory() as db:
                sub = db.get(Submission, submission_id)
                if sub is None:
                    raise NotFound("no such submission")
                view = submission_view(sub, db.get(Result, (sub.id, sub.tests_version)))
            ttl = s.status_ttl_pending_s if view["status"] == QUEUED else s.status_ttl_final_s
            self.cache.set_json(self._status_key(submission_id), view, ttl)
        if not user.is_staff and view["user_id"] != user.id:
            raise NotFound("no such submission")  # 404, not 403: do not reveal that it exists
        return view

    def history(self, user: User, assignment_id: Optional[int] = None, user_id: Optional[int] = None,
                limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        """Students see their own attempts; teachers and admins can filter by user."""
        stmt = select(Submission)
        if user.is_staff:
            if user_id is not None:
                stmt = stmt.where(Submission.user_id == user_id)
        else:
            stmt = stmt.where(Submission.user_id == user.id)
        if assignment_id is not None:
            stmt = stmt.where(Submission.assignment_id == assignment_id)
        stmt = stmt.order_by(Submission.id.desc()).limit(max(1, min(limit, 100))).offset(max(0, offset))
        with self.session_factory() as db:
            return [submission_view(sub) for sub in db.scalars(stmt)]

    # ------------------------------------------------------------------ submission.graded consumer
    def handle_graded(self, topic: str, key: Optional[bytes], value: Optional[bytes],
                      headers: dict) -> None:
        event = parse_event(value, "submission_id", "tests_version", "verdict")
        p = event["payload"]
        try:
            submission_id = int(p["submission_id"])
            version = int(p["tests_version"])
            passed = int(p.get("tests_passed") or 0)
            total = int(p.get("tests_total") or 0)
            time_ms = int(p.get("time_ms") or 0)
        except (TypeError, ValueError) as exc:
            raise PoisonMessage("a numeric field is not a number") from exc
        details = p.get("tests") if isinstance(p.get("tests"), list) else []
        graded_at = utcnow()
        if isinstance(p.get("graded_at"), str):
            try:
                graded_at = as_utc(datetime.fromisoformat(p["graded_at"])) or graded_at
            except ValueError:
                pass
        token = correlation_id_var.set(event.get("correlation_id") or "-")
        try:
            self.apply_result(submission_id, version, str(p["verdict"])[:32], passed, total, time_ms,
                              details, graded_at)
        finally:
            correlation_id_var.reset(token)

    def apply_result(self, submission_id: int, version: int, verdict: str, passed: int, total: int,
                     time_ms: int, details: list, graded_at: Optional[datetime] = None) -> bool:
        """Stores a verdict. True when it was applied, False when it was a duplicate, stale or for
        an unknown submission. At-least-once delivery makes duplicates normal, not an error."""
        with self.session_factory() as db:
            sub = db.get(Submission, submission_id)
            if sub is None:
                log.warning("verdict for an unknown submission %s ignored", submission_id)
                return False
            if version < sub.tests_version:
                log.info("stale verdict for %s (tests v%s < v%s) ignored", submission_id, version,
                         sub.tests_version)
                return False
            if db.get(Result, (submission_id, version)) is not None:
                return False                      # duplicate delivery
            db.add(Result(submission_id=submission_id, tests_version=version, verdict=verdict,
                          tests_passed=passed, tests_total=total, time_ms=time_ms, details=details,
                          graded_at=graded_at or utcnow()))
            sub.status = verdict
            sub.verdict = verdict
            sub.tests_version = version
            try:
                db.commit()
            except IntegrityError:                # a parallel consumer inserted the same result
                db.rollback()
                return False
            assignment_id, user_id = sub.assignment_id, sub.user_id
            self.cache.delete(self._status_key(submission_id))
            self._refresh_leaderboard(db, assignment_id, user_id)
        metrics.GRADED.labels(verdict).inc()
        return True

    # ------------------------------------------------------------------ UC-06: leaderboard
    def _scores_from_db(self, db: Session, assignment_id: int,
                        user_id: Optional[int] = None) -> dict[int, int]:
        """Best score per user, counting only the verdict of each submission under its CURRENT
        tests_version (so a regrade can lower a score)."""
        stmt = (select(Submission.user_id, Result.tests_passed, Result.tests_total)
                .join(Result, Result.submission_id == Submission.id)
                .where(Submission.assignment_id == assignment_id,
                       Result.tests_version == Submission.tests_version))
        if user_id is not None:
            stmt = stmt.where(Submission.user_id == user_id)
        best: dict[int, int] = {}
        for uid, passed, total in db.execute(stmt):
            best[uid] = max(best.get(uid, 0), score_of(passed, total))
        return best

    def _refresh_leaderboard(self, db: Session, assignment_id: int, user_id: int) -> None:
        key = self._leaderboard_key(assignment_id)
        if not self.cache.exists(key):
            return                                # nothing cached: the next read rebuilds it whole
        best = self._scores_from_db(db, assignment_id, user_id).get(user_id)
        if best is None:
            self.cache.zset_remove(key, str(user_id))
        else:
            self.cache.zset_set(key, str(user_id), best)

    def leaderboard(self, user: User, assignment_id: int, limit: int = LEADERBOARD_DEFAULT) -> dict[str, Any]:
        key = self._leaderboard_key(assignment_id)
        limit = max(1, min(limit, 100))
        if self.cache.exists(key):
            top = self.cache.ztop(key, limit)
            mine = self.cache.zrank_score(key, str(user.id))
        else:                                     # lost or never built: rebuild from PostgreSQL
            with self.session_factory() as db:
                scores = self._scores_from_db(db, assignment_id)
            self.cache.zset_replace(key, {str(uid): float(score) for uid, score in scores.items()})
            # same order as ZREVRANGE: score descending, ties by member descending
            ordered = sorted(((str(uid), float(score)) for uid, score in scores.items()),
                             key=lambda kv: (kv[1], kv[0]), reverse=True)
            top = ordered[:limit]
            mine = next(((i, sc) for i, (m, sc) in enumerate(ordered, start=1) if m == str(user.id)), None)
        return {
            "assignment_id": assignment_id,
            "entries": [{"rank": i, "user_id": int(m), "score": int(sc)}
                        for i, (m, sc) in enumerate(top, start=1)],
            "me": {"rank": mine[0], "score": int(mine[1])} if mine else None,
        }

    # ------------------------------------------------------------------ UC-05: regrade
    def regrade(self, user: User, assignment_id: int, correlation_id: str = "-") -> dict[str, Any]:
        """Re-publishes `submission.created` for every submission that is still queued or was
        graded under older tests. Submissions already graded under the current version are left
        alone: the grader's verdict would be a duplicate and the status would stay `queued`."""
        if not user.is_staff:
            raise Forbidden("only teachers can start a regrade")
        card = self._card(assignment_id, fresh=True)
        if user.role != "admin" and card.get("owner_id") != user.id:
            raise Forbidden("this is not your course")
        if not self.cache.set_if_absent(f"regrade:{assignment_id}", "1", REGRADE_LOCK_S):
            raise Conflict("a regrade of this assignment was just started, wait a minute")
        version = int(card["tests_version"])
        queued = skipped = 0
        last_id = 0
        while True:
            with self.session_factory() as db:
                rows = db.scalars(
                    select(Submission)
                    .where(Submission.assignment_id == assignment_id, Submission.id > last_id,
                           or_(Submission.status == QUEUED, Submission.tests_version < version))
                    .order_by(Submission.id).limit(REGRADE_BATCH)).all()
                if not rows:
                    break
                for sub in rows:
                    last_id = sub.id
                    code = self.sources.get(sub.id)
                    if code is None:              # source expired (NFR-11) or storage was reset
                        skipped += 1
                        continue
                    sub.status = QUEUED
                    sub.verdict = None
                    sub.tests_version = version
                    db.add(self._created_event(sub, code, correlation_id, regrade=True))
                    queued += 1
                db.commit()
                ids = [sub.id for sub in rows]
            self.cache.delete(*[self._status_key(i) for i in ids])
        return {"assignment_id": assignment_id, "tests_version": version, "queued": queued,
                "skipped_no_source": skipped}
