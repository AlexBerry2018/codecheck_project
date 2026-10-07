"""Message handlers of grader-service. No Kafka, Docker or database imports here: everything
is injected, so the whole flow (grade, retry, DLQ) is unit-testable."""
from __future__ import annotations

import logging
import time
from typing import Any, Callable, Optional

from . import metrics
from .config import Settings
from .errors import AssignmentNotFound, CoursesUnavailable
from .events import PoisonMessage, dumps, make_event, parse_event, utcnow
from .grading import grade_submission
from .logging_setup import correlation_id_var
from .models import SYSTEM_ERROR, Grade
from .sandbox import SandboxError

log = logging.getLogger("grader.worker")

MAX_WAIT_S = 60.0


def _to_int(value: Optional[str], default: int) -> int:
    try:
        return int(value) if value not in (None, "") else default
    except ValueError:
        return default


def _to_float(value: Optional[str], default: float = 0.0) -> float:
    try:
        return float(value) if value not in (None, "") else default
    except ValueError:
        return default


class GraderWorker:
    def __init__(self, settings: Settings, executor, provider, run_logs, publisher,
                 sleep: Callable[[float], Any] = time.sleep,
                 clock: Callable[[], float] = time.time) -> None:
        self.settings = settings
        self.executor = executor
        self.provider = provider
        self.run_logs = run_logs
        self.publisher = publisher
        self.sleep = sleep
        self.clock = clock

    # ------------------------------------------------------------------ submission.created[.retry]
    def handle_submission(self, topic: str, key: Optional[bytes], value: Optional[bytes],
                          headers: dict) -> None:
        event = parse_event(value, "submission_id", "assignment_id", "user_id", "code")
        payload = event["payload"]
        corr = event.get("correlation_id") or "-"
        token = correlation_id_var.set(corr)
        try:
            self._wait_until_due(headers)
            attempt = _to_int(headers.get("attempt"), 0)
            started = time.monotonic()
            try:
                grade, used_version = self._grade(payload)
            except AssignmentNotFound:
                log.error("assignment %s not found, giving a system_error verdict",
                          payload["assignment_id"])
                self._publish_graded(payload, _system_error(), _to_int(str(payload.get("tests_version")), 1),
                                     corr, error="assignment not found")
                return
            except (SandboxError, CoursesUnavailable) as exc:
                self._on_transient_failure(topic, value, headers, attempt, exc, payload, corr)
                return
            metrics.GRADE_SECONDS.observe(time.monotonic() - started)
            metrics.GRADED.labels(grade.verdict).inc()
            self._save_logs(payload, used_version, grade)
            self._publish_graded(payload, grade, used_version, corr)
            log.info("graded", extra={"ctx": {"submission_id": payload["submission_id"],
                                              "verdict": grade.verdict,
                                              "tests": f"{grade.tests_passed}/{grade.tests_total}"}})
        finally:
            correlation_id_var.reset(token)

    def _wait_until_due(self, headers: dict) -> None:
        not_before = _to_float(headers.get("not_before_ms")) / 1000
        delay = not_before - self.clock()
        if delay > 0:
            self.sleep(min(delay, MAX_WAIT_S))

    def _grade(self, payload: dict) -> tuple[Grade, int]:
        tests = self.provider.get(payload["assignment_id"], _to_int(str(payload.get("tests_version")), 1))
        grade = grade_submission(payload["code"], tests.cases, tests.time_limit_ms,
                                 tests.memory_limit_mb, self.executor, self.settings.max_parallel)
        return grade, tests.tests_version

    def _save_logs(self, payload: dict, tests_version: int, grade: Grade) -> None:
        try:
            self.run_logs.save(payload["submission_id"], payload["assignment_id"], tests_version, grade.logs)
        except Exception:                          # noqa: BLE001 - logs are nice to have, the verdict is not
            log.exception("could not save run logs")

    def _publish_graded(self, payload: dict, grade: Grade, tests_version: int, corr: str,
                        error: Optional[str] = None) -> None:
        body: dict[str, Any] = {
            "submission_id": payload["submission_id"],
            "assignment_id": payload["assignment_id"],
            "user_id": payload["user_id"],
            "tests_version": tests_version,
            "verdict": grade.verdict,
            "tests_passed": grade.tests_passed,
            "tests_total": grade.tests_total,
            "time_ms": grade.time_ms,
            "tests": grade.details,
            "graded_at": utcnow().isoformat(),
        }
        if error:
            body["error"] = error
        self.publisher.publish(self.settings.topic_graded, str(payload["submission_id"]),
                               dumps(make_event("submission.graded", body, corr)))

    def _on_transient_failure(self, topic: str, value: Optional[bytes], headers: dict, attempt: int,
                              exc: Exception, payload: dict, corr: str) -> None:
        attempt += 1
        sid = str(payload["submission_id"])
        if attempt >= self.settings.max_attempts:
            log.error("giving up on %s after %d attempts: %s", sid, attempt, exc)
            metrics.DLQ.inc()
            self.publisher.publish(self.settings.topic_dlq, sid, value or b"",
                                   {"attempts": attempt, "error": str(exc)[:200], "source_topic": topic})
            # the student must not wait forever: tell submissions-service the verdict is a system error
            self._publish_graded(payload, _system_error(), _to_int(str(payload.get("tests_version")), 1),
                                 corr, error=str(exc)[:200])
            return
        delay_s = self.settings.retry_delay_s * attempt
        log.warning("temporary failure for %s (attempt %d), retry in %d s: %s", sid, attempt, delay_s, exc)
        metrics.RETRIES.inc()
        self.publisher.publish(self.settings.topic_retry, sid, value or b"",
                               {"attempt": attempt, "not_before_ms": int((self.clock() + delay_s) * 1000),
                                "error": str(exc)[:200]})

    # ------------------------------------------------------------------ assignment.updated
    def handle_assignment_updated(self, topic: str, key: Optional[bytes], value: Optional[bytes],
                                  headers: dict) -> None:
        event = parse_event(value, "assignment_id", "tests_version")
        payload = event["payload"]
        try:
            version = int(payload["tests_version"])
        except (TypeError, ValueError) as exc:
            raise PoisonMessage("tests_version is not a number") from exc
        self.provider.invalidate(str(payload["assignment_id"]), version)


def _system_error() -> Grade:
    return Grade(verdict=SYSTEM_ERROR, tests_passed=0, tests_total=0, time_ms=0, details=[], logs=[])
