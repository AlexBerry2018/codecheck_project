"""API tests against an in-memory SQLite database (no Kafka, no PostgreSQL)."""
import json

import pytest

pytest.importorskip("sqlalchemy")

from sqlalchemy import select  # noqa: E402

from app.config import Settings  # noqa: E402
from app.factory import create_app  # noqa: E402
from app.models import Outbox  # noqa: E402
from app.outbox import OutboxRelay  # noqa: E402

TESTS = [
    {"stdin": "1 2", "expected_stdout": "3"},
    {"stdin": "10 20", "expected_stdout": "30", "is_hidden": True},
]


@pytest.fixture()
def app():
    settings = Settings(database_url="sqlite://", internal_token="tok", teacher_invite_code="inv",
                        admin_email="root@example.com", admin_password="rootpassword")
    return create_app(settings, start_relay=False)


@pytest.fixture()
def client(app):
    return app.test_client()


def register(client, email, role="student", **extra):
    body = {"email": email, "password": "password123", "role": role, **extra}
    return client.post("/api/auth/register", json=body)


def login(client, email, password="password123"):
    resp = client.post("/api/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200, resp.get_json()
    return {"Authorization": "Bearer " + resp.get_json()["access_token"]}


def teacher_with_assignment(client):
    assert register(client, "t@example.com", "teacher", invite_code="inv").status_code == 201
    teacher = login(client, "t@example.com")
    course = client.post("/api/courses", json={"title": "Python"}, headers=teacher).get_json()
    resp = client.post(f"/api/courses/{course['id']}/assignments", headers=teacher,
                       json={"title": "Sum", "tests": TESTS})
    assert resp.status_code == 201, resp.get_json()
    return teacher, course, resp.get_json()


def outbox_rows(app):
    with app.extensions["session_factory"]() as db:
        return [r.payload for r in db.scalars(select(Outbox).order_by(Outbox.id))]


def test_register_login_refresh(client):
    assert register(client, "a@example.com").status_code == 201
    assert register(client, "a@example.com").status_code == 409
    assert register(client, "b@example.com", role="admin").status_code == 422
    assert client.post("/api/auth/login", json={"email": "a@example.com", "password": "wrong-one"}).status_code == 401
    pair = client.post("/api/auth/login", json={"email": "a@example.com", "password": "password123"}).get_json()
    again = client.post("/api/auth/refresh", json={"refresh_token": pair["refresh_token"]})
    assert again.status_code == 200 and again.get_json()["access_token"]
    assert client.post("/api/auth/refresh", json={"refresh_token": pair["access_token"]}).status_code == 401


def test_teacher_needs_the_invite_code(client):
    assert register(client, "t@example.com", "teacher").status_code == 403
    assert register(client, "t@example.com", "teacher", invite_code="bad").status_code == 403
    assert register(client, "t@example.com", "teacher", invite_code="inv").status_code == 201


def test_admin_is_seeded_from_the_environment(client):
    admin = login(client, "root@example.com", "rootpassword")
    assert client.post("/api/courses", json={"title": "x"}, headers=admin).status_code == 201


def test_students_cannot_manage_and_see_only_public_tests(client):
    teacher, course, assignment = teacher_with_assignment(client)
    assert register(client, "s@example.com").status_code == 201
    student = login(client, "s@example.com")

    assert client.post("/api/courses", json={"title": "x"}, headers=student).status_code == 403
    assert client.get(f"/api/assignments/{assignment['id']}", headers=student).status_code == 403  # not enrolled
    assert client.post(f"/api/courses/{course['id']}/enroll", headers=student).status_code == 201
    assert client.post(f"/api/courses/{course['id']}/enroll", headers=student).status_code == 200  # idempotent

    seen = client.get(f"/api/assignments/{assignment['id']}", headers=student).get_json()
    assert [t["stdin"] for t in seen["tests"]] == ["1 2"]
    assert seen["tests_total"] == 2 and seen["hidden_tests"] == 1
    full = client.get(f"/api/assignments/{assignment['id']}", headers=teacher).get_json()
    assert len(full["tests"]) == 2


def test_only_the_owner_can_edit(client):
    teacher, course, assignment = teacher_with_assignment(client)
    assert register(client, "other@example.com", "teacher", invite_code="inv").status_code == 201
    other = login(client, "other@example.com")
    assert client.put(f"/api/assignments/{assignment['id']}", json={"is_open": False}, headers=other).status_code == 403
    assert client.post(f"/api/courses/{course['id']}/assignments", headers=other,
                       json={"title": "x", "tests": TESTS}).status_code == 403


def test_validation_errors_are_reported(client):
    teacher, course, _ = teacher_with_assignment(client)
    resp = client.post(f"/api/courses/{course['id']}/assignments", headers=teacher,
                       json={"title": "", "tests": [], "time_limit_ms": 5})
    assert resp.status_code == 422 and len(resp.get_json()["error"]["details"]) == 3
    assert client.put("/api/assignments/1", json={}, headers=teacher).status_code == 422


def test_events_are_written_to_the_outbox_in_the_same_transaction(app, client):
    teacher, _, assignment = teacher_with_assignment(client)
    rows = outbox_rows(app)
    assert len(rows) == 1
    assert rows[0]["type"] == "assignment.updated"
    assert rows[0]["payload"] == {"assignment_id": assignment["id"], "course_id": assignment["course_id"],
                                  "tests_version": 1}

    url = f"/api/assignments/{assignment['id']}"
    # deadline / is_open do not change grading: same version, no event
    assert client.put(url, json={"is_open": False, "deadline": "2030-01-01T00:00:00Z"}, headers=teacher).status_code == 200
    assert len(outbox_rows(app)) == 1
    # new tests or new limits: version 2 and a new event; version 3 for the limit change
    resp = client.put(url, json={"tests": [{"stdin": "", "expected_stdout": "x"}]}, headers=teacher)
    assert resp.get_json()["tests_version"] == 2 and resp.get_json()["tests_total"] == 1
    resp = client.put(url, json={"time_limit_ms": 3000}, headers=teacher)
    assert resp.get_json()["tests_version"] == 3
    assert [r["payload"]["tests_version"] for r in outbox_rows(app)] == [1, 2, 3]


def test_internal_endpoint_matches_what_the_grader_expects(client):
    _, _, assignment = teacher_with_assignment(client)
    url = f"/internal/assignments/{assignment['id']}"
    assert client.get(url).status_code == 403
    head = {"X-Internal-Token": "tok"}

    card = client.get(url, headers=head).get_json()
    assert "tests" not in card and card["owner_id"] and card["assignment_id"] == assignment["id"]

    full = client.get(url + "?tests=1", headers=head).get_json()
    assert full["tests_version"] == 1 and full["time_limit_ms"] == 2000 and full["memory_limit_mb"] == 128
    assert [(t["index"], t["is_hidden"]) for t in full["tests"]] == [(0, False), (1, True)]
    assert client.get("/internal/assignments/999", headers=head).status_code == 404


def test_relay_marks_delivered_rows_and_keeps_the_rest(app, client):
    teacher, _, assignment = teacher_with_assignment(client)
    url = f"/api/assignments/{assignment['id']}"
    for limit in (3000, 4000):
        client.put(url, json={"time_limit_ms": limit}, headers=teacher)

    class Broker:
        def __init__(self, fail_after):
            self.sent, self.fail_after = [], fail_after

        def publish(self, topic, key, value, headers=None):
            if len(self.sent) >= self.fail_after:
                raise RuntimeError("broker down")
            self.sent.append((topic, key, json.loads(value)))

    factory = app.extensions["session_factory"]
    flaky = OutboxRelay(factory, Broker(fail_after=1), stop=None)
    with pytest.raises(RuntimeError):
        flaky.drain_once()                                    # 1 delivered, the 2nd failed
    healthy = Broker(fail_after=99)
    assert OutboxRelay(factory, healthy, stop=None).drain_once() == 2      # only the 2 left
    assert [m[2]["payload"]["tests_version"] for m in healthy.sent] == [2, 3]
    assert healthy.sent[0][0] == "assignment.updated" and healthy.sent[0][1] == str(assignment["id"])
    assert OutboxRelay(factory, healthy, stop=None).drain_once() == 0
