"""Exact per-user limit for POST /api/submissions (NFR-9: 10 per minute). Kong cannot count
per user in its open-source edition (it can only key on IP, header or consumer), so Kong
applies a coarse per-token limit and this service enforces the real one."""
from __future__ import annotations

import time
from typing import Optional


def check_user_rate(cache, user_id: int, limit: int, now: Optional[float] = None,
                    window_s: int = 60) -> Optional[int]:
    """Counts one attempt in the current fixed window. Returns None when the attempt is allowed
    (or the cache is down: fail-open) and the number of seconds to wait when it is not."""
    if limit <= 0:
        return None
    moment = time.time() if now is None else now
    bucket = int(moment // window_s)
    count = cache.incr_window(f"rl:sub:{user_id}:{bucket}", window_s + 1)
    if count is None or count <= limit:
        return None
    return max(1, int(window_s - (moment % window_s)))
