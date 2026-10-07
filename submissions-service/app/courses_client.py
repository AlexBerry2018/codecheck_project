"""The only synchronous dependency of submissions-service: the card of an assignment (deadline,
limits, tests_version, owner) from courses-service, cached in Valkey for 60 s. When
courses-service is down the cached card keeps submissions flowing."""
from __future__ import annotations

from typing import Optional

import httpx


class CoursesUnavailable(Exception):
    """courses-service did not answer (or answered with a server error) and nothing is cached."""


class CoursesClient:
    def __init__(self, base_url: str, internal_token: str, cache, ttl_s: int, timeout_s: float = 3.0) -> None:
        self._client = httpx.Client(base_url=base_url, timeout=timeout_s,
                                    headers={"X-Internal-Token": internal_token})
        self._cache = cache
        self._ttl_s = ttl_s

    @staticmethod
    def key(assignment_id: int) -> str:
        return f"assignment:{assignment_id}"

    def get_assignment(self, assignment_id: int, fresh: bool = False) -> Optional[dict]:
        """The card, or None when the assignment does not exist. `fresh` skips the cache
        (regrade needs the current tests_version)."""
        if not fresh:
            cached = self._cache.get_json(self.key(assignment_id))
            if cached:
                return cached
        try:
            resp = self._client.get(f"/internal/assignments/{assignment_id}")
        except httpx.HTTPError as exc:
            raise CoursesUnavailable(str(exc)) from exc
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            raise CoursesUnavailable(f"courses-service answered {resp.status_code}")
        card = resp.json()
        self._cache.set_json(self.key(assignment_id), card, self._ttl_s)
        return card

    def close(self) -> None:
        self._client.close()
