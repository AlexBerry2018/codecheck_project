from datetime import timezone

import pytest

from app import validation as v


def test_register_normalises_email_and_defaults_to_student():
    clean, errors = v.validate_register({"email": "  Ann@Example.COM ", "password": "longenough"})
    assert errors == []
    assert clean == {"email": "ann@example.com", "password": "longenough", "role": "student"}


@pytest.mark.parametrize("data", [
    {}, {"email": "no-at", "password": "longenough"}, {"email": "a@b.co", "password": "short"},
    {"email": "a@b.co", "password": "longenough", "role": "admin"},   # admin is never self-service
    {"email": 5, "password": "longenough"},
])
def test_register_rejects_bad_input(data):
    clean, errors = v.validate_register(data)
    assert clean == {} and errors


def test_assignment_create_applies_defaults():
    fields, tests, errors = v.validate_assignment(
        {"title": " Sum ", "tests": [{"stdin": "1 2", "expected_stdout": "3"}]})
    assert errors == []
    assert fields == {"title": "Sum", "time_limit_ms": 2000, "memory_limit_mb": 128}
    assert tests == [{"stdin": "1 2", "expected_stdout": "3", "is_hidden": False}]


def test_assignment_create_requires_title_and_tests():
    _, tests, errors = v.validate_assignment({})
    assert len(errors) == 2 and tests == []


@pytest.mark.parametrize("name,value", [
    ("time_limit_ms", 50), ("time_limit_ms", 10_001), ("time_limit_ms", True),
    ("memory_limit_mb", 8), ("memory_limit_mb", 512), ("memory_limit_mb", "128"),
])
def test_assignment_limits_are_range_checked(name, value):
    _, _, errors = v.validate_assignment(
        {"title": "t", "tests": [{"expected_stdout": "x"}], name: value})
    assert any(name in e for e in errors)


def test_tests_are_validated_one_by_one():
    _, _, errors = v.validate_assignment({"title": "t", "tests": [
        {"stdin": "ok", "expected_stdout": "ok"}, {"stdin": 1, "expected_stdout": "x"}, "nope",
        {"stdin": "", "expected_stdout": "x" * (v.MAX_TEST_BYTES + 1)}]})
    assert [e.split(":")[0] for e in errors] == ["tests[1]", "tests[2]", "tests[3]"]


def test_too_many_tests():
    tests = [{"expected_stdout": "x"}] * (v.MAX_TESTS + 1)
    _, _, errors = v.validate_assignment({"title": "t", "tests": tests})
    assert errors


def test_partial_update_touches_only_given_keys():
    fields, tests, errors = v.validate_assignment({"is_open": False, "deadline": None}, partial=True)
    assert errors == [] and tests is None
    assert fields == {"is_open": False, "deadline": None}


def test_partial_update_with_new_tests():
    fields, tests, errors = v.validate_assignment({"tests": [{"expected_stdout": "1"}]}, partial=True)
    assert errors == [] and fields == {} and len(tests) == 1


def test_deadline_parsing():
    assert v.parse_deadline("2030-01-02T03:04:05Z").tzinfo == timezone.utc
    assert v.parse_deadline("2030-01-02T03:04:05").tzinfo == timezone.utc      # naive = UTC
    with pytest.raises(ValueError):
        v.parse_deadline("tomorrow")
    with pytest.raises(ValueError):
        v.parse_deadline(20300102)
    _, _, errors = v.validate_assignment({"deadline": "tomorrow"}, partial=True)
    assert errors
