"""Builds the real adapters, starts the sandbox and the Kafka consumers, stops them again."""
from __future__ import annotations

import logging
import threading
from typing import Optional

from . import metrics
from .cache import SafeCache
from .config import Settings
from .courses_client import CoursesClient
from .kafka_io import ConsumerLoop, Publisher
from .logging_setup import setup_logging
from .provider import AssignmentProvider
from .run_logs_store import RunLogStore
from .sandbox import SandboxError, make_executor
from .worker import GraderWorker

log = logging.getLogger("grader.runtime")


class Runtime:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.stop_event = threading.Event()
        self.consumers: list[ConsumerLoop] = []
        self._boot: Optional[threading.Thread] = None
        self._consumers_started = False
        self.executor = make_executor(settings.sandbox_mode, settings.sandbox_image)

    def start(self) -> None:
        s = self.settings
        setup_logging("grader-service")
        cache = SafeCache(s.valkey_url)
        self.courses = CoursesClient(s.courses_url, s.internal_token)
        provider = AssignmentProvider(cache, self.courses, s.tests_cache_ttl_s)
        self.run_logs = RunLogStore(s.mongo_url, s.mongo_db, s.run_logs_ttl_days)
        self.publisher = Publisher(s.kafka_bootstrap, "grader-service")
        self.worker = GraderWorker(s, self.executor, provider, self.run_logs, self.publisher,
                                   sleep=self.stop_event.wait)
        self._boot = threading.Thread(target=self._boot_sequence, name="grader-boot", daemon=True)
        self._boot.start()

    def _boot_sequence(self) -> None:
        """Consumers start only when the sandbox works; otherwise every message would just bounce
        through the retry topic."""
        s = self.settings
        while not self.stop_event.is_set():
            try:
                if hasattr(self.executor, "calibrate"):
                    self.executor.calibrate()
                break
            except SandboxError as exc:
                log.error("sandbox is not ready: %s (retrying in 5 s)", exc)
                self.stop_event.wait(5)
        if self.stop_event.is_set():
            return
        try:
            self.run_logs.ensure_index()
        except Exception:                                   # noqa: BLE001
            log.exception("could not create the run_logs index now, will retry on first write")

        def lag(group: str, topic: str, partition: int, value: int) -> None:
            metrics.CONSUMER_LAG.labels(group, topic, str(partition)).set(value)

        self.consumers = [
            ConsumerLoop("grader-submissions", s.kafka_bootstrap, "grader",
                         [s.topic_created, s.topic_retry], self.worker.handle_submission,
                         self.stop_event, on_lag=lag, poison_counter=metrics.POISON),
            ConsumerLoop("grader-assignments", s.kafka_bootstrap, "grader-assignments",
                         [s.topic_assignment_updated], self.worker.handle_assignment_updated,
                         self.stop_event, on_lag=lag, poison_counter=metrics.POISON),
        ]
        for consumer in self.consumers:
            consumer.start()
        self._consumers_started = True
        log.info("grader consumers started")

    def ready(self) -> bool:
        return (self._consumers_started and getattr(self.executor, "ready", False)
                and all(c.is_alive() for c in self.consumers))

    def stop(self) -> None:
        self.stop_event.set()
        for consumer in self.consumers:
            consumer.join(timeout=10)
        for closer in (getattr(self, "publisher", None), getattr(self, "courses", None),
                       getattr(self, "run_logs", None)):
            try:
                if closer is not None:
                    closer.close()
            except Exception:                               # noqa: BLE001
                pass
