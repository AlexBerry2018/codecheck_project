"""Thin wrappers over confluent-kafka.

Delivery guarantees (NFR-5, NFR-6):
  * producer: acks=all + idempotence, `publish` returns only after the broker confirmed;
  * consumer: auto-commit off. The offset is committed after the handler finished, so a
    crash re-delivers the message (at-least-once) and handlers must be idempotent.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional

from confluent_kafka import Consumer, KafkaError, KafkaException, Producer

from .events import PoisonMessage

log = logging.getLogger("grader.kafka")

Handler = Callable[[str, Optional[bytes], Optional[bytes], dict], None]


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


def _headers_to_dict(raw) -> dict:
    out = {}
    for k, v in raw or []:
        out[k] = v.decode("utf-8", "replace") if isinstance(v, (bytes, bytearray)) else (v or "")
    return out


class ConsumerLoop(threading.Thread):
    """One consumer, one thread, messages handled one by one in partition order."""

    def __init__(self, name: str, bootstrap: str, group: str, topics: list[str], handler: Handler,
                 stop: threading.Event,
                 on_lag: Optional[Callable[[str, str, int, int], None]] = None,
                 poison_counter=None) -> None:
        super().__init__(name=name, daemon=True)
        self.bootstrap = bootstrap
        self.group = group
        self.topics = topics
        self.handler = handler
        self.stop_event = stop
        self.on_lag = on_lag
        self.poison_counter = poison_counter
        self.subscribed = threading.Event()

    # ------------------------------------------------------------------
    def _make_consumer(self) -> Consumer:
        return Consumer({
            "bootstrap.servers": self.bootstrap,
            "group.id": self.group,
            "enable.auto.commit": False,
            "auto.offset.reset": "earliest",
            # a grading run can take tens of seconds; poll() is called between messages only
            "max.poll.interval.ms": 600000,
            "session.timeout.ms": 45000,
            "partition.assignment.strategy": "cooperative-sticky",
        })

    def run(self) -> None:
        consumer = None
        while not self.stop_event.is_set():
            try:
                consumer = self._make_consumer()
                consumer.subscribe(self.topics)
                self.subscribed.set()
                self._loop(consumer)
            except KafkaException:
                log.exception("consumer %s failed, restarting in 5 s", self.name)
                self.stop_event.wait(5)
            finally:
                if consumer is not None:
                    try:
                        consumer.close()
                    except Exception:             # noqa: BLE001
                        pass
                    consumer = None

    def _loop(self, consumer: Consumer) -> None:
        last_lag = 0.0
        while not self.stop_event.is_set():
            msg = consumer.poll(1.0)
            if msg is None:
                pass
            elif msg.error():
                if msg.error().code() != KafkaError._PARTITION_EOF:
                    log.warning("kafka error: %s", msg.error())
                    self.stop_event.wait(1)
            else:
                if not self._handle(msg):
                    return                        # stopping: leave the message uncommitted
                consumer.commit(message=msg, asynchronous=False)
            if self.on_lag is not None and time.monotonic() - last_lag > 15:
                last_lag = time.monotonic()
                self._report_lag(consumer)

    def _handle(self, msg) -> bool:
        """True when the message is finished (handled or skipped), False when we are stopping.
        A failing handler is retried with a growing delay: losing the event is worse than
        waiting, and a poison message has its own escape hatch (PoisonMessage)."""
        delay = 1.0
        while not self.stop_event.is_set():
            try:
                self.handler(msg.topic(), msg.key(), msg.value(), _headers_to_dict(msg.headers()))
                return True
            except PoisonMessage as exc:
                log.warning("skipping malformed message %s[%s]@%s: %s", msg.topic(), msg.partition(),
                            msg.offset(), exc)
                if self.poison_counter is not None:
                    self.poison_counter.inc()
                return True
            except Exception:                     # noqa: BLE001 - handler contract: retry
                log.exception("handler failed for %s[%s]@%s, retrying in %.0f s", msg.topic(),
                              msg.partition(), msg.offset(), delay)
                self.stop_event.wait(delay)
                delay = min(delay * 2, 30.0)
        return False

    def _report_lag(self, consumer: Consumer) -> None:
        try:
            for tp in consumer.assignment():
                _low, high = consumer.get_watermark_offsets(tp, timeout=2, cached=True)
                pos = consumer.position([tp])[0].offset
                lag = high - pos if pos >= 0 else 0
                self.on_lag(self.group, tp.topic, tp.partition, max(lag, 0))
        except Exception:                         # noqa: BLE001 - metrics must never break consuming
            log.debug("could not compute consumer lag", exc_info=True)
