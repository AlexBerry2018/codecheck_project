"""Full run output (stdout, stderr, timing) per test, kept in MongoDB for 90 days (NFR-11)."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from pymongo import MongoClient

log = logging.getLogger("grader.run_logs")


class RunLogStore:
    def __init__(self, url: str, db_name: str, ttl_days: int) -> None:
        self._client = MongoClient(url, serverSelectionTimeoutMS=3000)
        self._col = self._client[db_name]["runs"]
        self._ttl_s = ttl_days * 86400
        self._index_ready = False

    def ensure_index(self) -> None:
        """TTL index: Mongo deletes a document `ttl` seconds after its `created_at`."""
        self._col.create_index("created_at", expireAfterSeconds=self._ttl_s, name="ttl_created_at")
        self._col.create_index([("submission_id", 1), ("tests_version", 1)], name="by_submission")
        self._index_ready = True

    def save(self, submission_id: str, assignment_id: str, tests_version: int, logs: list[dict]) -> None:
        if not logs:
            return
        if not self._index_ready:
            self.ensure_index()
        now = datetime.now(timezone.utc)
        docs = [{**entry, "submission_id": submission_id, "assignment_id": assignment_id,
                 "tests_version": tests_version, "created_at": now} for entry in logs]
        self._col.insert_many(docs)

    def close(self) -> None:
        self._client.close()
