"""Where the grader gets the tests of an assignment from: Valkey first, then courses-service."""
from __future__ import annotations

from .models import AssignmentTests


class AssignmentProvider:
    def __init__(self, cache, courses, ttl_s: int) -> None:
        self._cache = cache
        self._courses = courses
        self._ttl_s = ttl_s

    @staticmethod
    def key(assignment_id: str, version: int) -> str:
        return f"tests:{assignment_id}:{version}"

    def get(self, assignment_id: str, requested_version: int) -> AssignmentTests:
        """Tests for `requested_version`. If courses-service already has newer tests, those
        are used and the returned `tests_version` says so: the verdict is stored under the
        version of the tests that were really run."""
        cached = self._cache.get_json(self.key(assignment_id, requested_version))
        if cached:
            return AssignmentTests.from_dict(cached)
        data = self._courses.get_assignment(assignment_id)      # AssignmentNotFound / CoursesUnavailable
        tests = AssignmentTests.from_dict(data)
        self._cache.set_json(self.key(assignment_id, tests.tests_version), tests.to_dict(), self._ttl_s)
        return tests

    def invalidate(self, assignment_id: str, current_version: int) -> None:
        """assignment.updated: drop the cached tests of every other version.
        The version is part of the key, so a stale entry could never be served as the new one;
        this only frees memory early."""
        self._cache.delete_matching(f"tests:{assignment_id}:*",
                                    keep=self.key(assignment_id, current_version))
