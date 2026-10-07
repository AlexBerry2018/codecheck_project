"""Transactional outbox. `emit` runs inside the request's transaction; `OutboxRelay` publishes
the rows afterwards. A crash between the two only delays an event, it never loses one, and a
duplicate is possible (consumers are idempotent)."""
from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import metrics
from .events import dumps, make_event
from .models import Outbox, utcnow

log = logging.getLogger("courses.outbox")


def emit(db: Session, topic: str, key: Any, event_type: str, payload: dict,
         correlation_id: Optional[str] = None) -> None:
    db.add(Outbox(topic=topic, key=str(key), payload=make_event(event_type, payload, correlation_id)))


class OutboxRelay(threading.Thread):
    def __init__(self, session_factory: Callable[[], Session], publisher, stop: threading.Event,
                 batch: int = 100, idle_s: float = 0.5) -> None:
        super().__init__(name="outbox-relay", daemon=True)
        self.session_factory = session_factory
        self.publisher = publisher
        self.stop_event = stop
        self.batch = batch
        self.idle_s = idle_s

    def drain_once(self) -> int:
        """Publish up to `batch` pending rows in id order; returns how many were delivered.
        Rows are locked with SKIP LOCKED, so several relays (replicas) never send the same row."""
        with self.session_factory() as db:
            rows = db.scalars(select(Outbox).where(Outbox.sent_at.is_(None)).order_by(Outbox.id)
                              .limit(self.batch).with_for_update(skip_locked=True)).all()
            sent = 0
            try:
                for row in rows:
                    self.publisher.publish(row.topic, row.key, dumps(row.payload))
                    row.sent_at = utcnow()
                    sent += 1
            finally:
                db.commit()               # keep the rows delivered so far even if a later one failed
            if sent:
                metrics.OUTBOX_PUBLISHED.inc(sent)
            return sent

    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                delivered = self.drain_once()
            except Exception:             # noqa: BLE001 - the relay must survive Kafka/DB outages
                metrics.OUTBOX_FAILURES.inc()
                log.exception("outbox relay failed, retrying in 2 s")
                self.stop_event.wait(2)
                continue
            if delivered == 0:
                self.stop_event.wait(self.idle_s)
