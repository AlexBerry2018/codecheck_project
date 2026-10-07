"""Valkey (Redis protocol) as a cache. Every call degrades instead of raising: when Valkey
is down the grader asks courses-service directly (design doc, "Кэширование")."""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Optional

import redis

log = logging.getLogger("grader.cache")


class SafeCache:
    def __init__(self, url: str) -> None:
        self._r = redis.Redis.from_url(url, decode_responses=True, socket_timeout=1.0,
                                       socket_connect_timeout=1.0, health_check_interval=30)
        self._last_warning = 0.0

    def _warn(self, exc: Exception) -> None:
        now = time.monotonic()
        if now - self._last_warning > 30:       # do not flood the log while Valkey is down
            self._last_warning = now
            log.warning("valkey unavailable, working without cache: %s", exc)

    def get_json(self, key: str) -> Optional[Any]:
        try:
            raw = self._r.get(key)
        except (redis.RedisError, OSError) as exc:
            self._warn(exc)
            return None
        return json.loads(raw) if raw else None

    def set_json(self, key: str, value: Any, ttl_s: int) -> None:
        try:
            self._r.set(key, json.dumps(value, ensure_ascii=False), ex=ttl_s)
        except (redis.RedisError, OSError) as exc:
            self._warn(exc)

    def delete_matching(self, pattern: str, keep: Optional[str] = None) -> None:
        try:
            for key in self._r.scan_iter(match=pattern, count=100):
                if key != keep:
                    self._r.delete(key)
        except (redis.RedisError, OSError) as exc:
            self._warn(exc)

    def ping(self) -> bool:
        try:
            return bool(self._r.ping())
        except (redis.RedisError, OSError):
            return False
