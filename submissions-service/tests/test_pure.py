"""Tests that need neither SQLAlchemy nor FastAPI: cache, rate limit, events, tokens."""
import time

import jwt
import pytest

from app.cache import MemoryCache
from app.events import PoisonMessage, dumps, make_event, parse_event
from app.ratelimit import check_user_rate
from app.security import decode_access_token
from app.sources import MemorySources

SECRET = "test-secret-test-secret-test-secret-0123"


def make_token(**overrides):
    claims = {"iss": "codecheck", "sub": "7", "role": "teacher", "type": "access",
              "exp": int(time.time()) + 60}
    claims.update(overrides)
    return jwt.encode(claims, SECRET, algorithm="HS256")


# ---------------------------------------------------------------- rate limit
def test_rate_limit_blocks_after_the_limit_and_reports_the_wait():
    cache = MemoryCache()
    assert [check_user_rate(cache, 1, 3, now=1000.0) for _ in range(3)] == [None, None, None]
    wait = check_user_rate(cache, 1, 3, now=1000.0)
    assert wait == 60 - (1000 % 60)                      # time left in the fixed window
    assert check_user_rate(cache, 2, 3, now=1000.0) is None   # other users are not affected


def test_rate_limit_resets_in_the_next_window():
    cache = MemoryCache()
    for _ in range(4):
        check_user_rate(cache, 1, 3, now=1000.0)
    assert check_user_rate(cache, 1, 3, now=1000.0 + 60) is None


def test_rate_limit_fails_open_when_the_cache_is_down():
    class Down:
        def incr_window(self, key, ttl_s):
            return None

    assert check_user_rate(Down(), 1, 1) is None
    assert check_user_rate(MemoryCache(), 1, 0) is None  # limit 0 = disabled


# ---------------------------------------------------------------- MemoryCache (the Valkey twin)
def test_ttl_expiry_uses_the_injected_clock():
    now = [0.0]
    cache = MemoryCache(clock=lambda: now[0])
    cache.set_json("status:1", {"a": 1}, ttl_s=3)
    assert cache.get_json("status:1") == {"a": 1}
    now[0] = 3.0
    assert cache.get_json("status:1") is None


def test_set_if_absent_is_a_lock():
    cache = MemoryCache()
    assert cache.set_if_absent("regrade:1", "1", 60) is True
    assert cache.set_if_absent("regrade:1", "1", 60) is False


def test_sorted_set_orders_by_score_then_member_like_zrevrange():
    cache = MemoryCache()
    cache.zset_replace("lb:1", {"1": 50.0, "2": 100.0, "3": 100.0})
    assert cache.ztop("lb:1", 2) == [("3", 100.0), ("2", 100.0)]
    assert cache.zrank_score("lb:1", "1") == (3, 50.0)
    assert cache.zrank_score("lb:1", "9") is None
    cache.zset_set("lb:1", "1", 100.0)
    assert cache.zrank_score("lb:1", "1") == (3, 100.0)
    cache.zset_remove("lb:1", "1")
    assert cache.zrank_score("lb:1", "1") is None


# ---------------------------------------------------------------- events
def test_event_envelope_has_the_documented_fields():
    event = make_event("submission.created", {"submission_id": 5}, "cid")
    assert set(event) == {"event_id", "type", "schema_version", "occurred_at", "correlation_id", "payload"}
    assert parse_event(dumps(event), "submission_id")["payload"] == {"submission_id": 5}


@pytest.mark.parametrize("raw", [None, b"", b"not json", b"[]", b'{"payload": 1}'])
def test_unparseable_messages_are_poison(raw):
    with pytest.raises(PoisonMessage):
        parse_event(raw)


def test_missing_payload_fields_are_poison():
    with pytest.raises(PoisonMessage):
        parse_event(dumps(make_event("x", {"a": 1})), "a", "b")


# ---------------------------------------------------------------- tokens and sources
def test_access_token_is_decoded_and_other_tokens_are_refused():
    user = decode_access_token(SECRET, "codecheck", make_token())
    assert (user.id, user.role, user.is_staff) == (7, "teacher", True)
    for bad in (make_token(type="refresh"), make_token(iss="someone"), make_token(exp=int(time.time()) - 5),
                make_token(sub="abc"), "garbage"):
        with pytest.raises(jwt.PyJWTError):
            decode_access_token(SECRET, "codecheck", bad)
    with pytest.raises(jwt.PyJWTError):
        decode_access_token("another-secret-another-secret-another", "codecheck", make_token())


def test_memory_sources_round_trip_and_outage():
    sources = MemorySources()
    sources.put(1, "print(1)")
    assert sources.get(1) == "print(1)" and sources.get(2) is None
    sources.fail = True
    with pytest.raises(RuntimeError):
        sources.put(3, "x")
