"""HTTP API of courses-service. Public routes go through Kong (/api/*); /internal/* is for the
other services only and is not routed by Kong."""
from __future__ import annotations

import hmac
from typing import Any

import jwt
from flask import Blueprint, current_app, g, jsonify, request
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from werkzeug.security import check_password_hash, generate_password_hash

from . import metrics, validation
from .models import Assignment, Case, Course, Enrollment, User, as_utc
from .outbox import emit
from .security import (REFRESH, auth_required, decode_token, error_response, internal_required,
                       issue_pair)

bp = Blueprint("api", __name__)


def settings():
    return current_app.config["SETTINGS"]


def json_body() -> dict[str, Any]:
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


# ------------------------------------------------------------------ views
def user_view(user: User) -> dict[str, Any]:
    return {"id": user.id, "email": user.email, "role": user.role}


def course_view(course: Course, enrolled: bool = False) -> dict[str, Any]:
    return {"id": course.id, "title": course.title, "owner_id": course.owner_id,
            "enrolled": enrolled, "mine": course.owner_id == g.user["id"]}


def assignment_view(a: Assignment, course: Course, with_tests: bool = False,
                    include_hidden: bool = False) -> dict[str, Any]:
    deadline = as_utc(a.deadline)
    body: dict[str, Any] = {
        "id": a.id, "course_id": a.course_id, "title": a.title, "description": a.description,
        "deadline": deadline.isoformat() if deadline else None,
        "time_limit_ms": a.time_limit_ms, "memory_limit_mb": a.memory_limit_mb,
        "tests_version": a.tests_version, "is_open": a.is_open,
        "tests_total": len(a.tests), "hidden_tests": sum(1 for t in a.tests if t.is_hidden),
    }
    if with_tests:
        body["tests"] = [{"index": t.position, "stdin": t.stdin, "expected_stdout": t.expected_stdout,
                          "is_hidden": t.is_hidden}
                         for t in a.tests if include_hidden or not t.is_hidden]
    return body


def _can_manage(course: Course) -> bool:
    return g.user["role"] == "admin" or (g.user["role"] == "teacher" and course.owner_id == g.user["id"])


def _can_view(course: Course) -> bool:
    return _can_manage(course) or g.db.get(Enrollment, (course.id, g.user["id"])) is not None


def _emit_assignment_updated(a: Assignment) -> None:
    emit(g.db, settings().topic_assignment_updated, a.id, "assignment.updated",
         {"assignment_id": a.id, "course_id": a.course_id, "tests_version": a.tests_version}, g.cid)


# ------------------------------------------------------------------ operations
@bp.get("/health/live")
def live():
    return {"status": "ok"}


@bp.get("/health/ready")
def ready():
    try:
        g.db.execute(text("select 1"))
    except Exception:                                        # noqa: BLE001
        return error_response(503, "not_ready", "database is unavailable")
    return {"status": "ok"}


@bp.get("/metrics")
def prometheus():
    body, content_type = metrics.render()
    return body, 200, {"Content-Type": content_type}


# ------------------------------------------------------------------ auth (UC-01)
@bp.post("/api/auth/register")
def register():
    data = json_body()
    clean, errors = validation.validate_register(data)
    if errors:
        return error_response(422, "validation", "invalid input", errors)
    if clean["role"] == "teacher":
        code = settings().teacher_invite_code
        if not code or not hmac.compare_digest(str(data.get("invite_code", "")), code):
            return error_response(403, "forbidden", "a valid invite_code is required for teachers")
    user = User(email=clean["email"], password_hash=generate_password_hash(clean["password"]),
                role=clean["role"])
    g.db.add(user)
    try:
        g.db.commit()
    except IntegrityError:
        g.db.rollback()
        return error_response(409, "email_taken", "this email is already registered")
    return jsonify(user_view(user)), 201


@bp.post("/api/auth/login")
def login():
    data = json_body()
    email = str(data.get("email", "")).strip().lower()
    user = g.db.scalar(select(User).where(User.email == email))
    if user is None or not check_password_hash(user.password_hash, str(data.get("password", ""))):
        return error_response(401, "invalid_credentials", "wrong email or password")
    return jsonify(issue_pair(settings(), user.id, user.role))


@bp.post("/api/auth/refresh")
def refresh():
    s = settings()
    try:
        claims = decode_token(s.jwt_secret, s.jwt_issuer, json_body().get("refresh_token"), REFRESH)
        user = g.db.get(User, int(claims["sub"]))
    except (jwt.PyJWTError, ValueError):
        user = None
    if user is None:
        return error_response(401, "unauthorized", "the refresh token is invalid or expired")
    return jsonify(issue_pair(s, user.id, user.role))


# ------------------------------------------------------------------ courses
@bp.get("/api/courses")
@auth_required()
def list_courses():
    enrolled = set(g.db.scalars(select(Enrollment.course_id).where(Enrollment.user_id == g.user["id"])))
    courses = g.db.scalars(select(Course).order_by(Course.id)).all()
    return jsonify([course_view(c, c.id in enrolled) for c in courses])


@bp.post("/api/courses")
@auth_required("teacher", "admin")
def create_course():
    title, errors = validation.validate_title(json_body().get("title"))
    if errors:
        return error_response(422, "validation", "invalid input", errors)
    course = Course(title=title, owner_id=g.user["id"])
    g.db.add(course)
    g.db.commit()
    return jsonify(course_view(course)), 201


@bp.post("/api/courses/<int:course_id>/enroll")
@auth_required("student")
def enroll(course_id: int):
    course = g.db.get(Course, course_id)
    if course is None:
        return error_response(404, "not_found", "no such course")
    if g.db.get(Enrollment, (course_id, g.user["id"])) is not None:
        return jsonify(course_view(course, enrolled=True)), 200
    g.db.add(Enrollment(course_id=course_id, user_id=g.user["id"]))
    try:
        g.db.commit()
    except IntegrityError:                                   # a parallel request enrolled the user
        g.db.rollback()
    return jsonify(course_view(course, enrolled=True)), 201


@bp.get("/api/courses/<int:course_id>/assignments")
@auth_required()
def list_assignments(course_id: int):
    course = g.db.get(Course, course_id)
    if course is None:
        return error_response(404, "not_found", "no such course")
    if not _can_view(course):
        return error_response(403, "forbidden", "enroll in the course first")
    items = g.db.scalars(select(Assignment).where(Assignment.course_id == course_id)
                         .order_by(Assignment.id)).all()
    return jsonify([assignment_view(a, course) for a in items])


# ------------------------------------------------------------------ assignments (UC-02)
@bp.post("/api/courses/<int:course_id>/assignments")
@auth_required("teacher", "admin")
def create_assignment(course_id: int):
    course = g.db.get(Course, course_id)
    if course is None:
        return error_response(404, "not_found", "no such course")
    if not _can_manage(course):
        return error_response(403, "forbidden", "this is not your course")
    fields, tests, errors = validation.validate_assignment(json_body(), partial=False)
    if errors:
        return error_response(422, "validation", "invalid input", errors)
    assignment = Assignment(course_id=course.id, tests_version=1, **fields)
    assignment.tests = [Case(position=i, **t) for i, t in enumerate(tests or [])]
    g.db.add(assignment)
    g.db.flush()                                             # assigns the id for the event
    _emit_assignment_updated(assignment)
    g.db.commit()
    return jsonify(assignment_view(assignment, course, with_tests=True, include_hidden=True)), 201


@bp.get("/api/assignments/<int:assignment_id>")
@auth_required()
def get_assignment(assignment_id: int):
    assignment = g.db.get(Assignment, assignment_id)
    course = g.db.get(Course, assignment.course_id) if assignment else None
    if assignment is None or course is None:
        return error_response(404, "not_found", "no such assignment")
    if not _can_view(course):
        return error_response(403, "forbidden", "enroll in the course first")
    # students see the public tests as examples; hidden tests stay with the teacher
    return jsonify(assignment_view(assignment, course, with_tests=True, include_hidden=_can_manage(course)))


@bp.put("/api/assignments/<int:assignment_id>")
@auth_required("teacher", "admin")
def update_assignment(assignment_id: int):
    assignment = g.db.get(Assignment, assignment_id)
    course = g.db.get(Course, assignment.course_id) if assignment else None
    if assignment is None or course is None:
        return error_response(404, "not_found", "no such assignment")
    if not _can_manage(course):
        return error_response(403, "forbidden", "this is not your course")
    fields, tests, errors = validation.validate_assignment(json_body(), partial=True)
    if not errors and not fields and tests is None:
        errors = ["nothing to update"]
    if errors:
        return error_response(422, "validation", "invalid input", errors)
    for name, value in fields.items():
        setattr(assignment, name, value)
    if tests is not None:
        assignment.tests = [Case(position=i, **t) for i, t in enumerate(tests)]
    # tests or limits changed => old verdicts were produced under different rules: new version
    if tests is not None or "time_limit_ms" in fields or "memory_limit_mb" in fields:
        assignment.tests_version += 1
        _emit_assignment_updated(assignment)
    g.db.commit()
    return jsonify(assignment_view(assignment, course, with_tests=True, include_hidden=True))


# ------------------------------------------------------------------ internal (grader, submissions)
@bp.get("/internal/assignments/<int:assignment_id>")
@internal_required
def internal_assignment(assignment_id: int):
    assignment = g.db.get(Assignment, assignment_id)
    course = g.db.get(Course, assignment.course_id) if assignment else None
    if assignment is None or course is None:
        return error_response(404, "not_found", "no such assignment")
    body = assignment_view(assignment, course, with_tests=request.args.get("tests") == "1",
                           include_hidden=True)
    body["assignment_id"] = assignment.id
    body["owner_id"] = course.owner_id
    return jsonify(body)
