"""HTTP API tests: FastAPI TestClient + in-memory SQLite + MemoryCache + MemorySources (no Kafka)."""
import time
import types

import pytest

pytest.importorskip("sqlalchemy")
pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.models import Outbox, Result, Submission  # noqa: E402
from tests.support import graded_bytes, make_app, token  # noqa: E402


@pytest.fixture()
def app():
    return make_app()


@pytest.fixture()
def client(app):
    return TestClient(app)


def submit(client, who=1, code="print(1)", assignment_id=1, key=None, role="student"):
    headers = token(who, role)
    if key:
        headers["Idempotency-Key"] = key
    return client.post("/api/submissions", data={"assignment_id": str(assignment_id), "code": code},
                       headers=headers)


def table(app, model, order):
    with app.state.session_factory() as db:
        return list(db.scalars(select(model).order_by(order)))


def outbox(app):
    return table(app, Outbox, Outbox.id)


def submissions(app):
    return table(app, Submission, Submission.id)


def grade(app, submission_id, **kwargs):
    app.state.service.handle_graded("submission.graded", None, graded_bytes(submission_id, **kwargs), {})


# ------------------------------------------------------------------ UC-03: submit
def test_submit_answers_202_and_queues_an_event_in_the_same_transaction(app, client):
    resp = submit(client)
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["status"] == "queued" and body["tests_version"] == 1 and body["user_id"] == 1
    assert resp.headers["location"] == f"/api/submissions/{body['id']}"
    [row] = outbox(app)
    assert (row.topic, row.key, row.sent_at) == ("submission.created", str(body["id"]), None)
    event = row.payload
    assert event["type"] == "submission.created" and event["schema_version"] == 1
    assert event["payload"] == {"submission_id": body["id"], "assignment_id": 1, "user_id": 1,
                                "tests_version": 1, "code": "print(1)"}
    assert app.state.sources.get(body["id"]) == "print(1)"


def test_submit_accepts_a_file_upload(app, client):
    resp = client.post("/api/submissions", data={"assignment_id": "1"}, headers=token(1),
                       files={"file": ("main.py", b"print(2)\n", "text/x-python")})
    assert resp.status_code == 202, resp.text
    assert app.state.sources.get(resp.json()["id"]) == "print(2)\n"


def test_submit_needs_exactly_one_of_file_and_code(client):
    both = client.post("/api/submissions", data={"assignment_id": "1", "code": "x"}, headers=token(1),
                       files={"file": ("main.py", b"x")})
    neither = client.post("/api/submissions", data={"assignment_id": "1"}, headers=token(1))
    missing = client.post("/api/submissions", data={"code": "x"}, headers=token(1))
    assert both.status_code == neither.status_code == missing.status_code == 422
    assert missing.json()["error"]["code"] == "validation"


def test_a_binary_file_is_refused(client):
    resp = client.post("/api/submissions", data={"assignment_id": "1"}, headers=token(1),
                       files={"file": ("a.bin", b"\xff\xfe\x00")})
    assert resp.status_code == 422


def test_empty_solution_is_422(client):
    assert submit(client, code="   \n").status_code == 422


def test_too_large_solution_is_413(app):
    small = TestClient(make_app(max_code_bytes=10))
    assert submit(small, code="x" * 11).status_code == 413
    big_file = small.post("/api/submissions", data={"assignment_id": "1"}, headers=token(1),
                          files={"file": ("main.py", b"x" * 11)})
    assert big_file.status_code == 413
    assert submit(small, code="x" * 10).status_code == 202


def test_requires_a_valid_access_token(client):
    form = {"assignment_id": "1", "code": "x"}
    assert client.post("/api/submissions", data=form).status_code == 401
    for header in ({"Authorization": "Bearer nonsense"},
                   token(1, type="refresh"),
                   token(1, exp=int(time.time()) - 10),
                   token(1, iss="somebody-else")):
        resp = client.post("/api/submissions", data=form, headers=header)
        assert resp.status_code == 401, header
        assert resp.json()["error"]["code"] == "unauthorized"


def test_unknown_assignment_is_404_and_courses_outage_is_503(app, client):
    assert submit(client, assignment_id=99).status_code == 404
    app.state.courses.down = True
    resp = submit(client)
    assert resp.status_code == 503 and resp.json()["error"]["code"] == "unavailable"


def test_closed_or_overdue_assignments_are_409(app, client):
    card = app.state.courses.cards[1]
    card["is_open"] = False
    closed = submit(client)
    assert closed.status_code == 409 and closed.json()["error"]["code"] == "assignment_closed"
    card.update(is_open=True, deadline="2000-01-01T00:00:00+00:00")
    assert submit(client).status_code == 409
    card["deadline"] = "2999-01-01T00:00:00+00:00"
    assert submit(client).status_code == 202


def test_source_storage_outage_leaves_no_trace(app, client):
    app.state.sources.fail = True
    resp = submit(client)
    assert resp.status_code == 503
    assert submissions(app) == [] and outbox(app) == []


# ------------------------------------------------------------------ FR-9: idempotency
def test_same_idempotency_key_returns_the_same_submission(app, client):
    first = submit(client, key="abc")
    second = submit(client, key="abc")
    assert first.status_code == second.status_code == 202
    assert first.json()["id"] == second.json()["id"]
    assert "idempotent-replay" not in first.headers and second.headers["idempotent-replay"] == "true"
    assert len(submissions(app)) == 1 and len(outbox(app)) == 1
    assert submit(client, who=2, key="abc").json()["id"] != first.json()["id"]   # keys are per user


def test_replay_works_even_when_the_cache_lost_the_key(app, client):
    first = submit(client, key="abc").json()["id"]
    app.state.cache.delete("idem:1:abc")
    assert submit(client, key="abc").json()["id"] == first


def test_key_reused_for_another_assignment_is_409(app, client):
    app.state.courses.cards[2] = dict(app.state.courses.cards[1], id=2, assignment_id=2)
    assert submit(client, key="abc").status_code == 202
    resp = submit(client, key="abc", assignment_id=2)
    assert resp.status_code == 409


# ------------------------------------------------------------------ NFR-9: per-user limit
def test_per_user_rate_limit(monkeypatch):
    # freeze the clock of the fixed window so the test cannot straddle a minute boundary
    monkeypatch.setattr("app.ratelimit.time", types.SimpleNamespace(time=lambda: 1_000_000.0))
    limited = TestClient(make_app(user_limit_per_min=2))
    assert [submit(limited).status_code for _ in range(3)] == [202, 202, 429]
    resp = submit(limited)
    assert resp.status_code == 429 and resp.json()["error"]["code"] == "rate_limited"
    assert int(resp.headers["retry-after"]) >= 1
    assert submit(limited, who=2).status_code == 202            # another user is not affected


def test_a_replay_does_not_use_up_the_quota(monkeypatch):
    monkeypatch.setattr("app.ratelimit.time", types.SimpleNamespace(time=lambda: 1_000_000.0))
    limited = TestClient(make_app(user_limit_per_min=1))
    assert submit(limited, key="k").status_code == 202
    assert submit(limited, key="k").status_code == 202           # replay: not counted
    assert submit(limited).status_code == 429


# ------------------------------------------------------------------ UC-04: status
def test_status_flow_and_visibility(app, client):
    sid = submit(client).json()["id"]
    queued = client.get(f"/api/submissions/{sid}", headers=token(1)).json()
    assert queued["status"] == "queued" and queued["verdict"] is None and "tests" not in queued

    grade(app, sid)                                   # also drops the cached `queued` view
    done = client.get(f"/api/submissions/{sid}", headers=token(1)).json()
    assert done["status"] == done["verdict"] == "accepted"
    assert (done["tests_passed"], done["tests_total"], done["time_ms"]) == (2, 2, 12)
    assert done["tests"][0]["verdict"] == "accepted"

    assert client.get(f"/api/submissions/{sid}", headers=token(2)).status_code == 404   # someone else's
    assert client.get(f"/api/submissions/{sid}", headers=token(10, "teacher")).status_code == 200
    assert client.get(f"/api/submissions/{sid}").status_code == 401
    assert client.get("/api/submissions/9999", headers=token(1)).status_code == 404


def test_history_is_private_for_students_and_filterable_for_teachers(app, client):
    mine = submit(client, who=1).json()["id"]
    submit(client, who=2)
    submit(client, who=1)
    own = client.get("/api/submissions", headers=token(1)).json()
    assert len(own) == 2 and {s["user_id"] for s in own} == {1} and own[0]["id"] > own[1]["id"]
    sneaky = client.get("/api/submissions", params={"user_id": 2}, headers=token(1)).json()
    assert {s["user_id"] for s in sneaky} == {1}                # the filter is ignored for students
    teacher = client.get("/api/submissions", params={"assignment_id": 1}, headers=token(10, "teacher")).json()
    assert len(teacher) == 3
    only_two = client.get("/api/submissions", params={"user_id": 2}, headers=token(10, "teacher")).json()
    assert [s["user_id"] for s in only_two] == [2]
    assert mine in [s["id"] for s in own]
    assert client.get("/api/submissions", params={"limit": 0}, headers=token(1)).status_code == 422


# ------------------------------------------------------------------ UC-06: leaderboard
def test_leaderboard_keeps_the_best_attempt_and_survives_cache_loss(app, client):
    s1 = submit(client, who=1).json()["id"]
    s2 = submit(client, who=2).json()["id"]
    s3 = submit(client, who=2).json()["id"]
    grade(app, s1, verdict="wrong_answer", passed=1, total=2)    # user 1: 50
    grade(app, s2)                                               # user 2: 100
    grade(app, s3, verdict="wrong_answer", passed=0, total=2)    # user 2 later fails: best stays 100

    board = client.get("/api/leaderboard/1", headers=token(1)).json()
    assert [(e["rank"], e["user_id"], e["score"]) for e in board["entries"]] == [(1, 2, 100), (2, 1, 50)]
    assert board["me"] == {"rank": 2, "score": 50}
    assert client.get("/api/leaderboard/1", headers=token(3)).json()["me"] is None

    s4 = submit(client, who=1).json()["id"]
    grade(app, s4)                                               # incremental update of a cached board
    scores = {e["user_id"]: e["score"] for e in client.get("/api/leaderboard/1", headers=token(1)).json()["entries"]}
    assert scores == {1: 100, 2: 100}

    app.state.cache.delete("lb:1")                               # Valkey lost the key: rebuilt from PostgreSQL
    again = client.get("/api/leaderboard/1", headers=token(1)).json()
    assert {e["user_id"]: e["score"] for e in again["entries"]} == {1: 100, 2: 100}


# ------------------------------------------------------------------ UC-05: regrade
def test_regrade_requeues_only_what_the_new_tests_make_obsolete(app, client):
    old = submit(client, who=1).json()["id"]
    grade(app, old)                                   # graded under tests v1: 100
    pending = submit(client, who=2).json()["id"]      # still queued
    assert client.get("/api/leaderboard/1", headers=token(1)).json()["entries"][0]["score"] == 100

    app.state.courses.cards[1]["tests_version"] = 2   # the teacher edited the tests
    resp = client.post("/api/submissions/assignments/1/regrade", headers=token(10, "teacher"))
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"assignment_id": 1, "tests_version": 2, "queued": 2, "skipped_no_source": 0}

    new_events = [r.payload["payload"] for r in outbox(app)[-2:]]
    assert {e["submission_id"] for e in new_events} == {old, pending}
    assert all(e["tests_version"] == 2 and e["regrade"] is True for e in new_events)
    again = client.get(f"/api/submissions/{old}", headers=token(1)).json()
    assert again["status"] == "queued" and again["tests_version"] == 2

    # a second click inside the lock window is refused
    assert client.post("/api/submissions/assignments/1/regrade", headers=token(10, "teacher")).status_code == 409

    # the grader answers: the stale v1 verdict is ignored, the v2 verdict replaces it and lowers the score
    grade(app, old, version=1)
    assert client.get(f"/api/submissions/{old}", headers=token(1)).json()["status"] == "queued"
    grade(app, old, version=2, verdict="wrong_answer", passed=1, total=2)
    final = client.get(f"/api/submissions/{old}", headers=token(1)).json()
    assert (final["status"], final["tests_version"], final["tests_passed"]) == ("wrong_answer", 2, 1)
    assert client.get("/api/leaderboard/1", headers=token(1)).json()["entries"][0]["score"] == 50


def test_regrade_is_for_the_owner_of_the_course_or_an_admin(app, client):
    submit(client)
    url = "/api/submissions/assignments/1/regrade"
    assert client.post(url, headers=token(1)).status_code == 403                      # student
    assert client.post(url, headers=token(11, "teacher")).status_code == 403          # someone else's course
    assert client.post("/api/submissions/assignments/99/regrade", headers=token(10, "teacher")).status_code == 404
    assert client.post(url, headers=token(1000, "admin")).status_code == 200


def test_regrade_skips_submissions_whose_source_is_gone(app, client):
    sid = submit(client).json()["id"]
    app.state.sources.docs.pop(sid)
    resp = client.post("/api/submissions/assignments/1/regrade", headers=token(10, "teacher"))
    assert resp.json()["queued"] == 0 and resp.json()["skipped_no_source"] == 1


# ------------------------------------------------------------------ operations
def test_health_metrics_and_error_shapes(client):
    assert client.get("/health/live").json() == {"status": "ok"}
    assert client.get("/health/ready").json() == {"status": "ok"}
    assert client.get("/metrics").status_code == 200
    missing = client.get("/no/such/route")
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "not_found"
    assert missing.headers["x-request-id"]


def test_results_table_is_filled_by_the_consumer_only(app, client):
    sid = submit(client).json()["id"]
    assert table(app, Result, Result.submission_id) == []
    grade(app, sid)
    [result] = table(app, Result, Result.submission_id)
    assert (result.submission_id, result.tests_version, result.verdict) == (sid, 1, "accepted")
