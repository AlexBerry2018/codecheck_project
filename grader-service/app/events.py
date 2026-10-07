"""Event envelope shared by all topics: event_id, type, schema_version, occurred_at,
correlation_id, payload (see "Каталог событий" in the design doc)."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Optional


class PoisonMessage(Exception):
    """The message can never be processed (bad JSON, missing fields): skip it, do not retry."""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def make_event(type_: str, payload: dict[str, Any], correlation_id: Optional[str] = None,
               schema_version: int = 1) -> dict[str, Any]:
    return {
        "event_id": str(uuid.uuid4()),
        "type": type_,
        "schema_version": schema_version,
        "occurred_at": utcnow().isoformat(),
        "correlation_id": correlation_id or "-",
        "payload": payload,
    }


def dumps(event: dict[str, Any]) -> bytes:
    return json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def parse_event(raw: Optional[bytes], *required_payload_fields: str) -> dict[str, Any]:
    if not raw:
        raise PoisonMessage("empty message")
    try:
        event = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise PoisonMessage(f"invalid JSON: {exc}") from exc
    if not isinstance(event, dict) or not isinstance(event.get("payload"), dict):
        raise PoisonMessage("event envelope without payload")
    missing = [f for f in required_payload_fields if f not in event["payload"]]
    if missing:
        raise PoisonMessage(f"payload misses fields: {', '.join(missing)}")
    return event
