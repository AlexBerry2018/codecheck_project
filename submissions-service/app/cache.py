"""Valkey (Redis protocol) as a cache, plus an in-memory twin for tests.

Every Cache call degrades instead of raising: when Valkey is down the service reads from
PostgreSQL and courses-service (slower, but it works) and skips rate limiting (fail-open)."""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Callable, Optional

log = logging.getLogger("submissions.cache")


class Cache:
    def __init__(self, url: str) -> None:
        import redis                                    # imported here: tests use MemoryCache
        self._redis = redis
        self._r = redis.Redis.from_url(url, decode_responses=True, socket_timeout=1.0,
                                       socket_connect_timeout=1.0, health_check_interval=30)
        self._last_warning = 0.0

    # ------------------------------------------------------------------ plumbing
    def _warn(self, exc: Exception) -> None:
        now = time.monotonic()
        if now - self._last_warning > 30:               # do not flood the log while Valkey is down
            self._last_warning = now
            log.warning("valkey unavailable, working without cache: %s", exc)

    def _call(self, fn: Callable[[], Any], default: Any) -> Any:
        try:
            return fn()
        except (self._redis.RedisError, OSError) as exc:
            self._warn(exc)
            return default

    # ------------------------------------------------------------------ keys
    def get_json(self, key: str) -> Optional[Any]:
        raw = self._call(lambda: self._r.get(key), None)
        return json.loads(raw) if raw else None

    def set_json(self, key: str, value: Any, ttl_s: int) -> None:
        self._call(lambda: self._r.set(key, json.dumps(value, ensure_ascii=False), ex=ttl_s), None)

    def delete(self, *keys: str) -> None:
        if keys:
            self._call(lambda: self._r.delete(*keys), None)

    def exists(self, key: str) -> bool:
        return bool(self._call(lambda: self._r.exists(key), 0))

    def set_if_absent(self, key: str, value: str, ttl_s: int) -> bool:
        """SET NX EX. True when the key was created (or when Valkey is down: fail-open)."""
        return bool(self._call(lambda: self._r.set(key, value, nx=True, ex=ttl_s), True))

    def incr_window(self, key: str, ttl_s: int) -> Optional[int]:
        """Counter for a fixed window; None when Valkey is down (callers must fail open)."""
        def run() -> int:
            pipe = self._r.pipeline()
            pipe.incr(key)
            pipe.expire(key, ttl_s, nx=True)
            return int(pipe.execute()[0])
        return self._call(run, None)

    # ------------------------------------------------------------------ sorted sets (leaderboard)
    def zset_replace(self, key: str, scores: dict[str, float]) -> None:
        def run() -> None:
            pipe = self._r.pipeline()
            pipe.delete(key)
            if scores:
                pipe.zadd(key, scores)
            pipe.execute()
        self._call(run, None)

    def zset_set(self, key: str, member: str, score: float) -> None:
        self._call(lambda: self._r.zadd(key, {member: score}), None)

    def zset_remove(self, key: str, member: str) -> None:
        self._call(lambda: self._r.zrem(key, member), None)

    def ztop(self, key: str, limit: int) -> list[tuple[str, float]]:
        return self._call(lambda: [(m, float(s)) for m, s in
                                   self._r.zrevrange(key, 0, limit - 1, withscores=True)], [])

    def zrank_score(self, key: str, member: str) -> Optional[tuple[int, float]]:
        """(1-based rank, score) of a member, None if absent."""
        def run() -> Optional[tuple[int, float]]:
            score = self._r.zscore(key, member)
            if score is None:
                return None
            return int(self._r.zrevrank(key, member)) + 1, float(score)
        return self._call(run, None)

    def ping(self) -> bool:
        return bool(self._call(lambda: self._r.ping(), False))


class MemoryCache:
    """Same interface, no Valkey. `clock` is injectable so TTLs can be tested."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self._data: dict[str, tuple[Any, Optional[float]]] = {}
        self._zsets: dict[str, dict[str, float]] = {}

    def _live(self, key: str) -> Optional[Any]:
        item = self._data.get(key)
        if item is None:
            return None
        value, expires = item
        if expires is not None and self.clock() >= expires:
            del self._data[key]
            return None
        return value

    def get_json(self, key: str) -> Optional[Any]:
        return self._live(key)

    def set_json(self, key: str, value: Any, ttl_s: int) -> None:
        self._data[key] = (json.loads(json.dumps(value)), self.clock() + ttl_s)

    def delete(self, *keys: str) -> None:
        for key in keys:
            self._data.pop(key, None)
            self._zsets.pop(key, None)

    def exists(self, key: str) -> bool:
        return self._live(key) is not None or key in self._zsets

    def set_if_absent(self, key: str, value: str, ttl_s: int) -> bool:
        if self._live(key) is not None:
            return False
        self._data[key] = (value, self.clock() + ttl_s)
        return True

    def incr_window(self, key: str, ttl_s: int) -> Optional[int]:
        current = self._live(key)
        if current is None:
            self._data[key] = (1, self.clock() + ttl_s)
            return 1
        self._data[key] = (current + 1, self._data[key][1])
        return current + 1

    def zset_replace(self, key: str, scores: dict[str, float]) -> None:
        self._zsets[key] = dict(scores)

    def zset_set(self, key: str, member: str, score: float) -> None:
        self._zsets.setdefault(key, {})[member] = score

    def zset_remove(self, key: str, member: str) -> None:
        self._zsets.get(key, {}).pop(member, None)

    def _ordered(self, key: str) -> list[tuple[str, float]]:
        # like Redis ZREVRANGE: score descending, ties by member descending
        return sorted(self._zsets.get(key, {}).items(), key=lambda kv: (kv[1], kv[0]), reverse=True)

    def ztop(self, key: str, limit: int) -> list[tuple[str, float]]:
        return self._ordered(key)[:limit]

    def zrank_score(self, key: str, member: str) -> Optional[tuple[int, float]]:
        for position, (m, score) in enumerate(self._ordered(key), start=1):
            if m == member:
                return position, score
        return None

    def ping(self) -> bool:
        return True
