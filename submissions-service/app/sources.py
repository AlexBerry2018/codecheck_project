"""Source code of the submissions, kept in MongoDB (one document per submission, read as a
whole, never joined): `sources.sources`, TTL 1 year (NFR-11). The source also travels inside
the `submission.created` event; this copy is what makes a regrade possible later."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger("submissions.sources")


class MongoSources:
    def __init__(self, url: str, db_name: str, ttl_days: int) -> None:
        from pymongo import MongoClient                 # imported here: tests use MemorySources
        self._client = MongoClient(url, serverSelectionTimeoutMS=3000)
        self._col = self._client[db_name]["sources"]
        self._ttl_s = ttl_days * 86400
        self._index_ready = False

    def ensure_index(self) -> None:
        self._col.create_index("created_at", expireAfterSeconds=self._ttl_s, name="ttl_created_at")
        self._index_ready = True

    def put(self, submission_id: int, code: str) -> None:
        if not self._index_ready:
            self.ensure_index()
        self._col.replace_one(
            {"_id": submission_id},
            {"_id": submission_id, "code": code, "size_bytes": len(code.encode("utf-8")),
             "created_at": datetime.now(timezone.utc)},
            upsert=True)

    def get(self, submission_id: int) -> Optional[str]:
        doc = self._col.find_one({"_id": submission_id})
        return doc["code"] if doc else None

    def ping(self) -> bool:
        try:
            self._client.admin.command("ping")
            return True
        except Exception:                               # noqa: BLE001
            return False

    def close(self) -> None:
        self._client.close()


class MemorySources:
    def __init__(self) -> None:
        self.docs: dict[int, str] = {}
        self.fail = False                               # tests flip this to simulate an outage

    def put(self, submission_id: int, code: str) -> None:
        if self.fail:
            raise RuntimeError("mongo is down")
        self.docs[submission_id] = code

    def get(self, submission_id: int) -> Optional[str]:
        return self.docs.get(submission_id)

    def ping(self) -> bool:
        return not self.fail

    def close(self) -> None:
        pass
