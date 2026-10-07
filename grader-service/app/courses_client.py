"""The grader's only synchronous dependency: read an assignment with its tests from
courses-service. The /internal/* routes are not published by Kong; the shared token is a
second line of defence."""
from __future__ import annotations

import httpx

from .errors import AssignmentNotFound, CoursesUnavailable


class CoursesClient:
    def __init__(self, base_url: str, internal_token: str, timeout_s: float = 3.0) -> None:
        self._client = httpx.Client(base_url=base_url, timeout=timeout_s,
                                    headers={"X-Internal-Token": internal_token})

    def get_assignment(self, assignment_id: str) -> dict:
        try:
            resp = self._client.get(f"/internal/assignments/{assignment_id}", params={"tests": 1})
        except httpx.HTTPError as exc:
            raise CoursesUnavailable(str(exc)) from exc
        if resp.status_code == 404:
            raise AssignmentNotFound(assignment_id)
        if resp.status_code != 200:
            raise CoursesUnavailable(f"courses-service answered {resp.status_code}")
        return resp.json()

    def close(self) -> None:
        self._client.close()
