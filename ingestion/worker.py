"""Continuous provider ingestion worker with bounded validation controls."""

from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from contextlib import ExitStack, contextmanager
from dataclasses import asdict
from datetime import timedelta
import json
import logging
from queue import Empty, LifoQueue
import signal
import threading
import time
from typing import Callable

import psycopg

from config.deployment_config import (
    DatabaseConfig,
    EUMETNET_MEMBER_COUNTRIES,
    MqttConfig,
    S3Config,
    TransformationConfig,
    WindyIngestionConfig,
    WorkerConfig,
)
from database.registry_queries import (
    DueSourceStream,
    EmaUpdateCandidate,
    apply_ema_update_candidates,
    get_due_source_streams,
)
from ingestion.notification.mqtt_publisher import MqttPublisher
from ingestion.shared.publication_outbox import drain_publication_outbox
from ingestion.windy.windy_image_access import WindyImageClient
from ingestion.windy.windy_ingestion_workflow import _process_job, _validate_countries
from ingestion.worker_metrics import HealthServer, WorkerHealth, WorkerMetrics
from storage.s3_storage import S3Storage


logger = logging.getLogger(__name__)


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

    @contextmanager
    def connection(self):
        started = time.monotonic()
        try:
            connection = self._available.get_nowait()
        except Empty:
            with self._lock:
                if self._created < self._max_size:
                    connection = _connect(self._config)
                    self._created += 1
                    self._all.append(connection)
                else:
                    connection = None
            if connection is None:
                connection = self._available.get()
        if self._observer is not None:
            self._observer("database_pool_wait", "success", time.monotonic() - started)
        try:
            yield connection
        finally:
            if getattr(connection, "closed", False):
                with self._lock:
                    self._created -= 1
                    self._all.remove(connection)
            else:
                self._available.put(connection)

    def close(self) -> None:
        for connection in self._all:
            connection.close()
        self._all.clear()


class LazyPooledConnection:
    """Borrow a database connection only when processing first writes state."""

    def __init__(self, pool: DatabaseConnectionPool) -> None:
        self._pool = pool
        self._context = None
        self._connection = None

    def __enter__(self):
        return self

    def __exit__(self, *exc: object) -> None:
        if self._context is not None:
            self._context.__exit__(*exc)

    def __getattr__(self, name: str):
        if self._connection is None:
            self._context = self._pool.connection()
            self._connection = self._context.__enter__()
        return getattr(self._connection, name)


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


def run_worker(
    *,
    network: str,
    countries: tuple[str, ...],
    max_jobs: int | None = None,
    epochs: int | None = None,
    dry_run: bool = False,
    stop_event: threading.Event | None = None,
    verbose: bool = False,
) -> list[dict[str, object]]:
    if network != "win":
        raise ValueError("checkpoint 7 supports only network 'win'")
    if epochs is not None and epochs < 1:
        raise ValueError("epochs must be positive")
    normalized = _validate_countries(countries)
    worker = WorkerConfig.from_environment()
    windy = WindyIngestionConfig.from_environment()
    maximum = max_jobs or worker.max_jobs_per_epoch
    stop = stop_event or threading.Event()
    metrics = WorkerMetrics()
    health = WorkerHealth(readiness_window_s=max(60.0, worker.failure_backoff_s * 3))
    server = HealthServer(worker.health_host, worker.health_port, health, metrics)
    server.start()
    gate = RateGate(windy.request_delay_s)
    summaries: list[dict[str, object]] = []
    epoch_index = 0
    try:
        while not stop.is_set() and (epochs is None or epoch_index < epochs):
            started = time.monotonic()
            try:
                summary = _run_epoch(
                    normalized,
                    maximum,
                    dry_run,
                    worker,
                    windy,
                    gate,
                    stop,
                    metrics,
                    epoch_index + 1,
                    verbose,
                )
                health.last_epoch_success_monotonic = time.monotonic()
                metrics.epochs.labels("win", "success").inc()
                duration_s = time.monotonic() - started
                summary["duration_s"] = round(duration_s, 6)
                summaries.append(summary)
                epoch_index += 1
                metrics.epoch_duration.labels("win").observe(duration_s)
                if epochs is None or epoch_index < epochs:
                    stop.wait(
                        _epoch_wait_s(
                            time.monotonic() - started,
                            worker.minimum_epoch_period_s,
                            worker.idle_delay_s,
                        )
                    )
            except Exception as error:
                logger.error("Windy ingestion epoch failed: %s", type(error).__name__)
                metrics.epochs.labels("win", "failure").inc()
                metrics.epoch_duration.labels("win").observe(time.monotonic() - started)
                if epochs is not None:
                    raise
                stop.wait(worker.failure_backoff_s)
    finally:
        health.intake_enabled = False
        server.close()
    return summaries


def _epoch_wait_s(
    elapsed_s: float, minimum_period_s: float, idle_delay_s: float
) -> float:
    """Enforce a minimum epoch start-to-start period and an idle floor."""
    return max(idle_delay_s, minimum_period_s - elapsed_s, 0)


def _run_epoch(
    countries: tuple[str, ...],
    max_jobs: int,
    dry_run: bool,
    worker: WorkerConfig,
    windy: WindyIngestionConfig,
    gate: Callable[[], None],
    stop: threading.Event,
    metrics: WorkerMetrics,
    epoch_number: int = 1,
    verbose: bool = False,
) -> dict[str, object]:
    epoch_started = time.monotonic()
    database = DatabaseConfig.from_environment()
    pool = DatabaseConnectionPool(
        database, worker.database_pool_size, metrics.observe_stage
    )
    with ExitStack() as resources:
        resources.callback(pool.close)
        client = resources.enter_context(
            WindyImageClient(
                windy.read_api_key(),
                request_timeout_s=windy.request_timeout_s,
                image_timeout_s=windy.image_download_timeout_s,
                max_image_bytes=windy.image_max_bytes,
                request_delay_s=0,
                retry_count=windy.download_retry_count,
                retry_backoff_s=windy.retry_backoff_s,
                request_gate=gate,
                observer=metrics.observe_stage,
            )
        )
        storage = (
            S3Storage(S3Config.from_environment(), observer=metrics.observe_stage)
            if not dry_run
            else None
        )
        publisher = (
            resources.enter_context(
                MqttPublisher(
                    MqttConfig.from_environment(), observer=metrics.observe_stage
                )
            )
            if not dry_run
            else None
        )
        if not dry_run:
            with pool.connection() as connection:
                assert storage is not None and publisher is not None
                deliveries = drain_publication_outbox(
                    connection, storage, publisher, limit=worker.outbox_batch_size
                )
            metrics.outbox.labels("win").set(
                sum(item.outcome != "published" for item in deliveries)
            )
            for item in deliveries:
                metrics.jobs.labels("win", item.outcome).inc()
        with pool.connection() as connection:
            jobs = get_due_source_streams(
                connection,
                "win",
                timedelta(seconds=windy.minimum_ingestion_interval_s),
                polling_interval_factor=windy.polling_interval_factor,
                maximum_poll_interval=timedelta(
                    seconds=windy.maximum_poll_interval_s
                ),
                countries=countries,
                limit=max_jobs,
            )
            connection.commit()
        metrics.selected.labels("win").set(len(jobs))
        if not jobs or stop.is_set():
            return {"selected": len(jobs), "outcomes": {}}
        results = []
        with ThreadPoolExecutor(max_workers=worker.threads, thread_name_prefix="windy") as executor:
            futures: set[Future] = {
                executor.submit(
                    _process_due_job,
                    job,
                    dry_run,
                    windy,
                    client,
                    pool,
                    storage,
                    publisher,
                    metrics.observe_stage,
                    metrics.observe_event,
                    True,
                )
                for job in jobs
            }
            last_report = 0.0
            while futures:
                done, futures = wait(futures, timeout=1, return_when=FIRST_COMPLETED)
                for future in done:
                    result = future.result()
                    results.append(result)
                    metrics.jobs.labels("win", result.outcome).inc()
                now = time.monotonic()
                if verbose and (now - last_report >= 1 or not futures):
                    print(json.dumps(_progress(epoch_number, len(jobs), results), sort_keys=True), flush=True)
                    last_report = now
        candidates = [
            result.ema_update_candidate
            for result in results
            if isinstance(result.ema_update_candidate, EmaUpdateCandidate)
        ]
        ema_updates_applied = 0
        ema_update_eligible = (
            not dry_run
            and epoch_number > 1
            and time.monotonic() - epoch_started
            < windy.minimum_ingestion_interval_s
        )
        if ema_update_eligible and candidates:
            with pool.connection() as connection:
                ema_updates_applied = apply_ema_update_candidates(
                    connection, candidates
                )
                connection.commit()
    outcomes: dict[str, int] = {}
    for result in results:
        outcomes[result.outcome] = outcomes.get(result.outcome, 0) + 1
    return {
        "selected": len(jobs),
        "outcomes": dict(sorted(outcomes.items())),
        "ema_candidates": len(candidates),
        "ema_updates_applied": ema_updates_applied,
    }


def _progress(epoch: int, selected: int, results: list[object]) -> dict[str, object]:
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


def _process_due_job(
    job: DueSourceStream,
    dry_run: bool,
    windy: WindyIngestionConfig,
    client: WindyImageClient,
    pool: DatabaseConnectionPool,
    storage: S3Storage | None,
    publisher: MqttPublisher | None,
    stage_observer,
    event_observer,
    defer_ema_update: bool = True,
):
    with LazyPooledConnection(pool) as connection:
        return _process_job(
            connection,
            client,
            job,
            dry_run=dry_run,
            ema_alpha=windy.ema_alpha,
            transformation=TransformationConfig.from_environment(),
            storage=storage,
            publisher=publisher,
            stage_observer=stage_observer,
            event_observer=event_observer,
            defer_ema_update=defer_ema_update,
        )


def _connect(config: DatabaseConfig):
    return psycopg.connect(
        host=config.host,
        port=config.port,
        dbname=config.name,
        user=config.user,
        password=config.read_password(),
        connect_timeout=5,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--network", default="win")
    parser.add_argument(
        "--countries", default=",".join(EUMETNET_MEMBER_COUNTRIES)
    )
    parser.add_argument("--max-jobs", type=int)
    parser.add_argument("--epochs", type=int)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--verbose", action="store_true", help="print epoch progress once per second"
    )
    args = parser.parse_args()
    stop = threading.Event()

    def request_stop(*_: object) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    summaries = run_worker(
        network=args.network,
        countries=tuple(args.countries.split(",")),
        max_jobs=args.max_jobs,
        epochs=args.epochs,
        dry_run=args.dry_run,
        stop_event=stop,
        verbose=args.verbose,
    )
    durations = [float(item["duration_s"]) for item in summaries]
    print(
        json.dumps(
            {
                "epochs": summaries,
                "mean_epoch_duration_s": (
                    round(sum(durations) / len(durations), 6) if durations else None
                ),
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
