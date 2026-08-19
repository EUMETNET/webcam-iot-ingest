"""Provider-neutral support for continuous ingestion workers."""

from __future__ import annotations

from collections import Counter
from concurrent.futures import Future
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import logging
from queue import Empty, LifoQueue
import threading
import time

import psycopg
from psycopg.pq import TransactionStatus

from config.deployment_config import DatabaseConfig
from database.registry_queries import DueSourceStream
from ingestion.shared.worker_metrics import WorkerMetrics


logger = logging.getLogger(__name__)


class InitialPollingStagger:
    """Deterministically spread each stream's first check without DB writes."""

    def __init__(
        self,
        seed: str,
        window_s: float,
        started_monotonic: float | None = None,
    ) -> None:
        if not seed:
            raise ValueError("initial polling stagger seed cannot be empty")
        if window_s <= 0:
            raise ValueError("initial polling stagger window must be positive")
        self.seed = seed
        self.window_s = window_s
        self.started_monotonic = (
            time.monotonic() if started_monotonic is None else started_monotonic
        )
        self._released: set[str] = set()

    def select(
        self,
        jobs: list[DueSourceStream],
        now_monotonic: float | None = None,
    ) -> tuple[list[DueSourceStream], int, set[str]]:
        elapsed_s = max(
            0.0,
            (time.monotonic() if now_monotonic is None else now_monotonic)
            - self.started_monotonic,
        )
        selected: list[DueSourceStream] = []
        released_now: set[str] = set()
        for job in jobs:
            already_released = job.source_stream_id in self._released
            if already_released or elapsed_s >= _initial_phase_s(
                job, self.seed, self.window_s
            ):
                self._released.add(job.source_stream_id)
                selected.append(job)
                if not already_released:
                    released_now.add(job.source_stream_id)
        return selected, len(jobs) - len(selected), released_now


def _initial_phase_s(job: DueSourceStream, seed: str, window_s: float) -> float:
    digest = hashlib.sha256(
        f"{seed}\0{job.source_stream_id}".encode("utf-8")
    ).digest()
    fraction = int.from_bytes(digest[:8], "big") / 2**64
    return fraction * window_s


class DatabaseConnectionPool:
    """Small bounded pool that avoids holding a connection during provider I/O."""

    def __init__(self, config: DatabaseConfig, max_size: int, observer=None) -> None:
        self._config = config
        self._max_size = max_size
        self._available: LifoQueue = LifoQueue()
        self._created = 0
        self._lock = threading.Lock()
        self._all: list[object] = []
        self._observer = observer
        self._discarded_slots = 0

    @contextmanager
    def connection(self):
        started = time.monotonic()
        try:
            connection = self._available.get_nowait()
        except Empty:
            with self._lock:
                if self._created < self._max_size:
                    connection = connect_database(self._config)
                    self._created += 1
                    self._all.append(connection)
                    if self._discarded_slots:
                        self._discarded_slots -= 1
                        if self._observer is not None:
                            self._observer(
                                "database_pool_cleanup",
                                "replacement",
                                time.monotonic() - started,
                            )
                else:
                    connection = None
            if connection is None:
                connection = self._available.get()
        if self._observer is not None:
            self._observer("database_pool_wait", "success", time.monotonic() - started)
        try:
            yield connection
        finally:
            if self._restore(connection):
                self._available.put(connection)

    def _restore(self, connection: object) -> bool:
        """Return whether a borrowed connection is safe to reuse."""

        if getattr(connection, "closed", False):
            self._discard(connection)
            return False
        started = time.monotonic()
        try:
            status = connection.info.transaction_status
            if status != TransactionStatus.IDLE:
                connection.rollback()
                if connection.info.transaction_status != TransactionStatus.IDLE:
                    raise RuntimeError("database connection rollback did not restore IDLE")
                if self._observer is not None:
                    self._observer(
                        "database_pool_cleanup",
                        "rollback",
                        time.monotonic() - started,
                    )
            return True
        except Exception:
            logger.exception("Discarding database connection that could not be restored")
            try:
                connection.close()
            except Exception:
                logger.exception("Failed to close discarded database connection")
            self._discard(connection)
            if self._observer is not None:
                self._observer(
                    "database_pool_cleanup",
                    "discard",
                    time.monotonic() - started,
                )
            return False

    def _discard(self, connection: object) -> None:
        with self._lock:
            if connection in self._all:
                self._all.remove(connection)
                self._created -= 1
                self._discarded_slots += 1

    def close(self) -> None:
        for connection in self._all:
            connection.close()
        self._all.clear()


@dataclass(frozen=True)
class InternalErrorResult:
    """Accounting result for an unexpected exception escaping a source job."""

    source_stream_id: str
    outcome: str = "internal_error"
    period_estimate_candidate: None = None


def completed_future_result(
    future: Future,
    job: DueSourceStream,
    *,
    network_id: str,
    epoch_number: int,
    metrics: WorkerMetrics,
):
    try:
        result = future.result()
    except Exception:
        logger.exception(
            "Unexpected source job failure: source_stream_id=%s network=%s epoch=%s",
            job.source_stream_id,
            network_id,
            epoch_number,
        )
        metrics.jobs.labels(network_id, "internal_error").inc()
        metrics.observe_event(
            "failure", {"stage": "source_job", "reason": "internal_error"}
        )
        return InternalErrorResult(job.source_stream_id)
    metrics.jobs.labels(network_id, result.outcome).inc()
    return result


class RateGate:
    """Serialize request starts across threads at a configured interval."""

    def __init__(self, interval_s: float) -> None:
        self._interval_s = interval_s
        self._lock = threading.Lock()
        self._next = 0.0

    def __call__(self) -> None:
        with self._lock:
            now = time.monotonic()
            wait = max(0.0, self._next - now)
            self._next = max(now, self._next) + self._interval_s
        if wait:
            time.sleep(wait)


def epoch_wait_s(
    elapsed_s: float, minimum_period_s: float, idle_delay_s: float
) -> float:
    """Enforce a minimum epoch start-to-start period and an idle floor."""
    return max(idle_delay_s, minimum_period_s - elapsed_s, 0)


def period_update_allowed(
    epoch_number: int,
    epoch_duration_s: float,
    minimum_ingestion_interval_s: float,
) -> bool:
    """Keep the established first-epoch and overlong-epoch period guards."""
    return epoch_number > 1 and epoch_duration_s < minimum_ingestion_interval_s


def progress(epoch: int, selected: int, results: list[object]) -> dict[str, object]:
    outcomes = Counter(result.outcome for result in results)
    downloaded = sum(
        outcomes[name]
        for name in ("downloaded", "published", "storage_error", "mqtt_error")
    )
    uploaded = outcomes["published"] + outcomes["mqtt_error"]
    return {
        "worker_progress": {
            "epoch": epoch,
            "position": f"{len(results)}/{selected}",
            "completed": len(results),
            "selected": selected,
            "downloaded": downloaded,
            "uploaded": uploaded,
            "throttled": outcomes["throttled"],
        }
    }


def connect_database(config: DatabaseConfig):
    return psycopg.connect(
        host=config.host,
        port=config.port,
        dbname=config.name,
        user=config.user,
        password=config.read_password(),
        connect_timeout=5,
    )
