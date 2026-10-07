"""`submission.graded` handling and the outbox relay: idempotency, stale events, poison messages."""
import threading

import pytest

pytest.importorskip("sqlalchemy")
pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.events import PoisonMessage, dumps, make_event  # noqa: E402
from app.models import Outbox, Result, Submission  # noqa: E402
from app.outbox import OutboxRelay  # noqa: E402
from tests.support import graded_bytes, make_app, token  # noqa: E402


@pytest.fixture()
def app():
    return make_app()


@pytest.fixture()
def client(app):
    return TestClient(app)


def new_submission(client, who=1):
    resp = client.post("/api/submissions", data={"assignment_id": "1", "code": "print(1)"}, headers=token(who))
    assert resp.status_code == 202, resp.text
    return resp.json()["id"]


def feed(app, raw):
    app.state.service.handle_graded("submission.graded", None, raw, {})


def results(app):
    with app.state.session_factory() as db:
        return list(db.scalars(select(Result).order_by(Result.submission_id, Result.tests_version)))


def submission(app, sid):
    with app.state.session_factory() as db:
        return db.get(Submission, sid)


def test_duplicate_events_are_applied_once(app, client):
    sid = new_submission(client)
    feed(app, graded_bytes(sid))
    feed(app, graded_bytes(sid))                      # at-least-once delivery: the same message again
    feed(app, graded_bytes(sid, verdict="wrong_answer", passed=0))   # same version, different content
    assert len(results(app)) == 1
    assert submission(app, sid).verdict == "accepted"


def test_a_stale_version_is_ignored_and_a_newer_one_wins(app, client):
    sid = new_submission(client)
    feed(app, graded_bytes(sid, version=2, verdict="wrong_answer", passed=1))   # the grader used newer tests
    feed(app, graded_bytes(sid, version=1))                                     # late answer for old tests
    assert [(r.tests_version, r.verdict) for r in results(app)] == [(2, "wrong_answer")]
    assert (submission(app, sid).tests_version, submission(app, sid).status) == (2, "wrong_answer")


def test_unknown_submissions_are_skipped_not_retried(app):
    feed(app, graded_bytes(424242))                   # must not raise: a raise would make the loop retry forever
    assert results(app) == []


def test_system_error_from_the_dead_letter_path_is_stored(app, client):
    sid = new_submission(client)
    feed(app, graded_bytes(sid, verdict="system_error", passed=0, total=0, tests=[], error="sandbox down"))
    assert submission(app, sid).status == "system_error"


@pytest.mark.parametrize("raw", [
    b"not json",
    dumps(make_event("submission.graded", {"submission_id": 1})),                      # fields missing
    dumps(make_event("submission.graded", {"submission_id": "x", "tests_version": 1, "verdict": "accepted"})),
    dumps(make_event("submission.graded", {"submission_id": 1, "tests_version": None, "verdict": "accepted"})),
])
def test_malformed_events_raise_poison_message(app, raw):
    with pytest.raises(PoisonMessage):
        feed(app, raw)


# ------------------------------------------------------------------ outbox relay
class FakePublisher:
    def __init__(self, fail_on=None):
        self.sent, self.fail_on = [], fail_on

    def publish(self, topic, key, value, headers=None, timeout_s=15.0):
        if self.fail_on is not None and len(self.sent) == self.fail_on:
            raise RuntimeError("broker is down")
        self.sent.append((topic, key, value))


def relay(app, publisher):
    return OutboxRelay(app.state.session_factory, publisher, threading.Event())


def test_relay_publishes_each_row_once_in_order(app, client):
    ids = [new_submission(client) for _ in range(3)]
    publisher = FakePublisher()
    r = relay(app, publisher)
    assert r.drain_once() == 3
    assert [key for _, key, _ in publisher.sent] == [str(i) for i in ids]
    assert {topic for topic, _, _ in publisher.sent} == {"submission.created"}
    assert r.drain_once() == 0                         # sent_at is set: nothing is published twice
    with app.state.session_factory() as db:
        assert all(row.sent_at is not None for row in db.scalars(select(Outbox)))


def test_relay_keeps_what_was_delivered_when_the_broker_fails_midway(app, client):
    for _ in range(3):
        new_submission(client)
    flaky = FakePublisher(fail_on=2)                   # the third publish fails
    with pytest.raises(RuntimeError):
        relay(app, flaky).drain_once()
    assert len(flaky.sent) == 2
    healthy = FakePublisher()
    assert relay(app, healthy).drain_once() == 1       # only the undelivered row is left
