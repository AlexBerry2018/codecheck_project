"""Producer for the outbox relay. acks=all + idempotence: `publish` returns only after the
broker confirmed the message (NFR-5)."""
from __future__ import annotations

import logging
from typing import Optional

from confluent_kafka import Producer

log = logging.getLogger("courses.kafka")


class PublishError(Exception):
    pass


class Publisher:
    def __init__(self, bootstrap: str, client_id: str) -> None:
        self._producer = Producer({
            "bootstrap.servers": bootstrap,
            "client.id": client_id,
            "acks": "all",
            "enable.idempotence": True,
            "linger.ms": 5,
        })

    def publish(self, topic: str, key: str, value: bytes, headers: Optional[dict] = None,
                timeout_s: float = 15.0) -> None:
        errors: list = []

        def on_delivery(err, _msg) -> None:
            if err is not None:
                errors.append(err)

        hdrs = [(k, str(v).encode("utf-8")) for k, v in (headers or {}).items()] or None
        try:
            self._producer.produce(topic, value=value, key=str(key).encode("utf-8"), headers=hdrs,
                                   on_delivery=on_delivery)
        except BufferError:                       # local queue is full: let it drain, try once more
            self._producer.poll(1.0)
            self._producer.produce(topic, value=value, key=str(key).encode("utf-8"), headers=hdrs,
                                   on_delivery=on_delivery)
        remaining = self._producer.flush(timeout_s)
        if remaining > 0 or errors:
            raise PublishError(f"could not deliver to {topic}: {errors[0] if errors else 'timeout'}")

    def close(self) -> None:
        self._producer.flush(5)
