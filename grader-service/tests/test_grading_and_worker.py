import json
import unittest

from app.config import Settings
from app.errors import AssignmentNotFound, CoursesUnavailable
from app.events import PoisonMessage, dumps, make_event
from app.grading import grade_submission
from app.models import (ACCEPTED, MEMORY_LIMIT, SYSTEM_ERROR, TIME_LIMIT, WRONG_ANSWER,
                        CaseSpec, RunResult)
from app.provider import AssignmentProvider
from app.sandbox import LocalExecutor, SandboxError
from app.worker import GraderWorker

CASES = [
    CaseSpec(1, "1 2\n", "3", False),
    CaseSpec(2, "10 20\n", "30", True),
    CaseSpec(3, "-5 5\n", "0", True),
]
SUM = "a, b = map(int, input().split())\nprint(a + b)"


class FakeExecutor:
    """Returns canned results keyed by the stdin of the test."""

    def __init__(self, fn):
        self.fn = fn
        self.calls = 0

    def run(self, code, stdin, time_limit_ms, memory_mb):
        self.calls += 1
        return self.fn(code, stdin)


def ok(stdout, ms=5):
    return RunResult(stdout=stdout, stderr="", exit_code=0, duration_ms=ms)


class GradingTests(unittest.TestCase):
    def test_all_accepted(self):
        grade = grade_submission(SUM, CASES, 2000, 64, LocalExecutor(), max_parallel=2)
        self.assertEqual((grade.verdict, grade.tests_passed, grade.tests_total), (ACCEPTED, 3, 3))
        self.assertEqual([d["index"] for d in grade.details], [1, 2, 3])

    def test_first_failure_decides_verdict_but_all_tests_run(self):
        ex = FakeExecutor(lambda code, stdin: ok("3") if stdin.startswith("1 ") else ok("WRONG"))
        grade = grade_submission("x", CASES, 2000, 64, ex)
        self.assertEqual(ex.calls, 3)
        self.assertEqual(grade.verdict, WRONG_ANSWER)
        self.assertEqual(grade.tests_passed, 1)

    def test_hidden_tests_leak_only_the_verdict(self):
        grade = grade_submission(SUM, CASES, 2000, 64, LocalExecutor())
        visible, hidden = grade.details[0], grade.details[1]
        self.assertIn("stdout", visible)
        self.assertEqual(set(hidden), {"index", "verdict", "time_ms", "hidden"})
        self.assertNotIn("30", json.dumps(hidden))

    def test_logs_keep_full_output_for_every_test(self):
        grade = grade_submission(SUM, CASES, 2000, 64, LocalExecutor())
        self.assertEqual(len(grade.logs), 3)
        self.assertEqual(grade.logs[1]["stdout"].strip(), "30")

    def test_time_is_the_slowest_test(self):
        ex = FakeExecutor(lambda c, s: ok("3" if s.startswith("1 ") else "30" if s.startswith("10") else "0",
                                          ms=7 if s.startswith("10") else 3))
        self.assertEqual(grade_submission("x", CASES, 2000, 64, ex).time_ms, 7)

    def test_time_limit_and_memory_limit_verdicts(self):
        slow = FakeExecutor(lambda c, s: RunResult("", "", -9, 2500, timed_out=True))
        self.assertEqual(grade_submission("x", CASES, 2000, 64, slow).verdict, TIME_LIMIT)
        oom = FakeExecutor(lambda c, s: RunResult("", "", 137, 100, oom=True))
        self.assertEqual(grade_submission("x", CASES, 2000, 64, oom).verdict, MEMORY_LIMIT)

    def test_no_tests_is_a_system_error(self):
        self.assertEqual(grade_submission(SUM, [], 2000, 64, LocalExecutor()).verdict, SYSTEM_ERROR)

    def test_sandbox_error_propagates(self):
        def boom(code, stdin):
            raise SandboxError("docker is down")
        with self.assertRaises(SandboxError):
            grade_submission("x", CASES, 2000, 64, FakeExecutor(boom))


class FakeCache:
    def __init__(self):
        self.data = {}

    def get_json(self, key):
        return self.data.get(key)

    def set_json(self, key, value, ttl_s):
        self.data[key] = value

    def delete_matching(self, pattern, keep=None):
        prefix = pattern.rstrip("*")
        for key in [k for k in self.data if k.startswith(prefix) and k != keep]:
            del self.data[key]


class FakeCourses:
    def __init__(self, version=1):
        self.version = version
        self.calls = 0
        self.fail = None

    def get_assignment(self, assignment_id):
        self.calls += 1
        if self.fail:
            raise self.fail
        return {"id": assignment_id, "tests_version": self.version, "time_limit_ms": 2000,
                "memory_limit_mb": 64,
                "tests": [{"index": c.index, "stdin": c.stdin, "expected_stdout": c.expected_stdout,
                           "is_hidden": c.is_hidden} for c in CASES]}


class ProviderTests(unittest.TestCase):
    def test_second_lookup_is_served_from_cache(self):
        cache, courses = FakeCache(), FakeCourses(version=3)
        provider = AssignmentProvider(cache, courses, 3600)
        first = provider.get("a1", 3)
        second = provider.get("a1", 3)
        self.assertEqual(courses.calls, 1)
        self.assertEqual(first.tests_version, 3)
        self.assertEqual(len(second.cases), 3)

    def test_newer_tests_win_and_are_cached_under_their_own_version(self):
        cache, courses = FakeCache(), FakeCourses(version=5)
        got = AssignmentProvider(cache, courses, 3600).get("a1", 4)
        self.assertEqual(got.tests_version, 5)
        self.assertIn("tests:a1:5", cache.data)

    def test_invalidate_keeps_only_the_current_version(self):
        cache = FakeCache()
        for v in (1, 2, 3):
            cache.data[f"tests:a1:{v}"] = {}
        cache.data["tests:other:1"] = {}
        AssignmentProvider(cache, FakeCourses(), 10).invalidate("a1", 3)
        self.assertEqual(sorted(cache.data), ["tests:a1:3", "tests:other:1"])

    def test_not_found_and_unavailable_propagate(self):
        courses = FakeCourses()
        courses.fail = AssignmentNotFound("a1")
        with self.assertRaises(AssignmentNotFound):
            AssignmentProvider(FakeCache(), courses, 10).get("a1", 1)
        courses.fail = CoursesUnavailable("down")
        with self.assertRaises(CoursesUnavailable):
            AssignmentProvider(FakeCache(), courses, 10).get("a1", 1)


class FakePublisher:
    def __init__(self):
        self.sent = []

    def publish(self, topic, key, value, headers=None, timeout_s=15.0):
        # the real producer calls key.encode(): an int key crashed the grader on a real broker
        assert isinstance(key, str), f"Kafka key must be str, got {type(key).__name__}"
        self.sent.append({"topic": topic, "key": key, "value": value, "headers": headers or {}})

    def topics(self):
        return [m["topic"] for m in self.sent]


class FakeRunLogs:
    def __init__(self):
        self.saved = []
        self.fail = False

    def save(self, submission_id, assignment_id, tests_version, logs):
        if self.fail:
            raise RuntimeError("mongo is down")
        self.saved.append((submission_id, tests_version, len(logs)))


def created_event(code=SUM, version=1, sid=101, corr="corr-1"):
    # submissions-service sends numeric ids (sub.id is an int), so the tests do the same
    return dumps(make_event("submission.created", {
        "submission_id": sid, "assignment_id": "a1", "user_id": "u1", "tests_version": version,
        "code": code}, corr))


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.settings = Settings(max_attempts=3, retry_delay_s=5)
        self.courses = FakeCourses()
        self.publisher = FakePublisher()
        self.logs = FakeRunLogs()
        self.now = 1_000_000.0
        self.slept = []
        self.executor = LocalExecutor()
        self.worker = self._worker(self.executor)

    def _worker(self, executor):
        provider = AssignmentProvider(FakeCache(), self.courses, 3600)
        return GraderWorker(self.settings, executor, provider, self.logs, self.publisher,
                            sleep=self.slept.append, clock=lambda: self.now)

    def graded(self):
        msgs = [m for m in self.publisher.sent if m["topic"] == "submission.graded"]
        return [json.loads(m["value"]) for m in msgs]

    def test_happy_path_publishes_graded_event_and_saves_logs(self):
        self.worker.handle_submission("submission.created", b"s1", created_event(), {})
        (event,) = self.graded()
        self.assertEqual(event["type"], "submission.graded")
        self.assertEqual(event["correlation_id"], "corr-1")
        p = event["payload"]
        self.assertEqual((p["verdict"], p["tests_passed"], p["tests_total"], p["tests_version"]),
                         (ACCEPTED, 3, 3, 1))
        self.assertEqual(self.publisher.sent[0]["key"], "101")
        self.assertEqual(self.logs.saved, [(101, 1, 3)])

    def test_wrong_solution(self):
        self.worker.handle_submission("submission.created", b"s1", created_event("print(1)"), {})
        self.assertEqual(self.graded()[0]["payload"]["verdict"], WRONG_ANSWER)

    def test_verdict_is_stored_under_the_version_of_the_tests_actually_used(self):
        self.courses.version = 7
        self.worker.handle_submission("submission.created", b"s1", created_event(version=6), {})
        self.assertEqual(self.graded()[0]["payload"]["tests_version"], 7)

    def test_log_store_failure_does_not_lose_the_verdict(self):
        self.logs.fail = True
        self.worker.handle_submission("submission.created", b"s1", created_event(), {})
        self.assertEqual(len(self.graded()), 1)

    def test_sandbox_failure_goes_to_retry_topic_with_backoff(self):
        def boom(code, stdin):
            raise SandboxError("docker is down")
        worker = self._worker(FakeExecutor(boom))
        raw = created_event()
        worker.handle_submission("submission.created", b"s1", raw, {})
        (msg,) = self.publisher.sent
        self.assertEqual(msg["topic"], "submission.created.retry")
        self.assertEqual(msg["value"], raw)                       # same event, untouched
        self.assertEqual(msg["headers"]["attempt"], 1)
        self.assertEqual(msg["headers"]["not_before_ms"], int((self.now + 5) * 1000))

    def test_retry_message_waits_until_it_is_due(self):
        due = int((self.now + 12) * 1000)
        self.worker.handle_submission("submission.created.retry", b"s1", created_event(),
                                      {"attempt": "1", "not_before_ms": str(due)})
        self.assertEqual(self.slept, [12.0])
        self.assertEqual(len(self.graded()), 1)

    def test_third_failure_goes_to_dlq_and_student_gets_system_error(self):
        def boom(code, stdin):
            raise SandboxError("docker is down")
        worker = self._worker(FakeExecutor(boom))
        worker.handle_submission("submission.created.retry", b"s1", created_event(), {"attempt": "2"})
        self.assertEqual(self.publisher.topics(), ["submission.created.dlq", "submission.graded"])
        self.assertEqual(self.graded()[0]["payload"]["verdict"], SYSTEM_ERROR)
        self.assertEqual(self.publisher.sent[0]["headers"]["attempts"], 3)

    def test_courses_outage_is_retried_too(self):
        self.courses.fail = CoursesUnavailable("503")
        self.worker.handle_submission("submission.created", b"s1", created_event(), {})
        self.assertEqual(self.publisher.topics(), ["submission.created.retry"])

    def test_missing_assignment_is_final(self):
        self.courses.fail = AssignmentNotFound("a1")
        self.worker.handle_submission("submission.created", b"s1", created_event(), {})
        self.assertEqual(self.publisher.topics(), ["submission.graded"])
        p = self.graded()[0]["payload"]
        self.assertEqual(p["verdict"], SYSTEM_ERROR)
        self.assertIn("not found", p["error"])

    def test_malformed_messages_are_poison(self):
        for raw in (b"", b"not json", b'{"payload": 1}', b'{"payload": {"submission_id": "s1"}}'):
            with self.assertRaises(PoisonMessage):
                self.worker.handle_submission("submission.created", None, raw, {})
        self.assertEqual(self.publisher.sent, [])

    def test_assignment_updated_drops_old_cache_entries(self):
        cache = FakeCache()
        cache.data.update({"tests:a1:1": {}, "tests:a1:2": {}})
        worker = GraderWorker(self.settings, self.executor, AssignmentProvider(cache, self.courses, 10),
                              self.logs, self.publisher)
        raw = dumps(make_event("assignment.updated", {"assignment_id": "a1", "tests_version": 2}))
        worker.handle_assignment_updated("assignment.updated", b"a1", raw, {})
        self.assertEqual(list(cache.data), ["tests:a1:2"])

    def test_assignment_updated_with_garbage_version_is_poison(self):
        raw = dumps(make_event("assignment.updated", {"assignment_id": "a1", "tests_version": "x"}))
        with self.assertRaises(PoisonMessage):
            self.worker.handle_assignment_updated("assignment.updated", b"a1", raw, {})


if __name__ == "__main__":
    unittest.main()
