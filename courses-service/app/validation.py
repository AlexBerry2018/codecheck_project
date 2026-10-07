"""Input validation without any framework or database: every function returns the cleaned
values plus a list of human-readable errors (empty list = valid)."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
SELF_SERVICE_ROLES = ("student", "teacher")

# name: (min, max, default); NFR-3 caps one run at 10 s and 256 MB
LIMITS = {"time_limit_ms": (100, 10_000, 2_000), "memory_limit_mb": (16, 256, 128)}
MAX_TESTS = 100
MAX_TEST_BYTES = 64 * 1024


def parse_deadline(value: Any) -> datetime:
    """ISO 8601 -> aware datetime (naive input is taken as UTC). Raises ValueError."""
    if not isinstance(value, str):
        raise ValueError("deadline must be an ISO 8601 string")
    parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def validate_register(data: dict[str, Any]) -> tuple[dict[str, str], list[str]]:
    errors: list[str] = []
    email = data.get("email")
    password = data.get("password")
    role = data.get("role", "student")
    if not isinstance(email, str) or not EMAIL_RE.match(email.strip()) or len(email) > 255:
        errors.append("email: a valid address up to 255 characters is required")
    if not isinstance(password, str) or not 8 <= len(password) <= 128:
        errors.append("password: 8..128 characters")
    if role not in SELF_SERVICE_ROLES:
        errors.append("role: student or teacher")
    if errors:
        return {}, errors
    return {"email": email.strip().lower(), "password": password, "role": role}, []


def validate_title(value: Any, field: str = "title") -> tuple[str, list[str]]:
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= 200:
        return "", [f"{field}: 1..200 characters"]
    return value.strip(), []


def _int_in_range(value: Any, lo: int, hi: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and lo <= value <= hi


def _validate_tests(raw: Any) -> tuple[list[dict[str, Any]], list[str]]:
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_TESTS:
        return [], [f"tests: a list of 1..{MAX_TESTS} items"]
    cleaned: list[dict[str, Any]] = []
    errors: list[str] = []
    for i, item in enumerate(raw):
        stdin = item.get("stdin", "") if isinstance(item, dict) else None
        expected = item.get("expected_stdout") if isinstance(item, dict) else None
        if not isinstance(stdin, str) or not isinstance(expected, str):
            errors.append(f"tests[{i}]: stdin (string) and expected_stdout (string) are required")
        elif len(stdin.encode()) > MAX_TEST_BYTES or len(expected.encode()) > MAX_TEST_BYTES:
            errors.append(f"tests[{i}]: stdin and expected_stdout are limited to {MAX_TEST_BYTES // 1024} KB")
        else:
            cleaned.append({"stdin": stdin, "expected_stdout": expected,
                            "is_hidden": bool(item.get("is_hidden", False))})
    return cleaned, errors


def validate_assignment(data: dict[str, Any], partial: bool = False
                        ) -> tuple[dict[str, Any], list[dict[str, Any]] | None, list[str]]:
    """partial=False: create (title and tests are required, limits get defaults).
    partial=True: update (only the given keys are validated and returned).
    Returns (column values, new tests or None when the tests are not touched, errors)."""
    errors: list[str] = []
    fields: dict[str, Any] = {}

    if not partial or "title" in data:
        title, errs = validate_title(data.get("title"))
        errors += errs
        if not errs:
            fields["title"] = title

    if "description" in data:
        if isinstance(data["description"], str) and len(data["description"]) <= 20_000:
            fields["description"] = data["description"]
        else:
            errors.append("description: a string up to 20000 characters")

    for name, (lo, hi, default) in LIMITS.items():
        if name in data:
            if _int_in_range(data[name], lo, hi):
                fields[name] = data[name]
            else:
                errors.append(f"{name}: an integer in {lo}..{hi}")
        elif not partial:
            fields[name] = default

    if "deadline" in data:
        if data["deadline"] is None:
            fields["deadline"] = None
        else:
            try:
                fields["deadline"] = parse_deadline(data["deadline"])
            except (ValueError, TypeError):
                errors.append("deadline: an ISO 8601 date-time or null")

    if "is_open" in data:
        if isinstance(data["is_open"], bool):
            fields["is_open"] = data["is_open"]
        else:
            errors.append("is_open: true or false")

    tests = None
    if "tests" in data or not partial:
        tests, errs = _validate_tests(data.get("tests"))
        errors += errs
    return fields, tests, errors
