"""Continuous Skaping ingestion worker with ETag freshness checks."""

from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from contextlib import ExitStack
from datetime import timedelta
import json
import logging
import signal
import threading
import time
from dataclasses import replace

from config.deployment_config import (
    DatabaseConfig,
    MqttConfig,
    S3Config,
    SkapingIngestionConfig,
    TransformationConfig,
    WorkerConfig,
)
from database.registry_queries import (
    EmaUpdateCandidate,
    apply_ingestion_state_updates,
    get_due_source_streams,
)
from ingestion.notification.mqtt_publisher import MqttPublisher
from ingestion.skaping.skaping_ingestion_workflow import _new_client
from ingestion.windy.windy_ingestion_workflow import _process_job
from ingestion.worker import (
    DatabaseConnectionPool,
    InitialPollingStagger,
    RateGate,
    _completed_future_result,
    _ema_update_allowed,
    _epoch_wait_s,
    _progress,
)
from ingestion.worker_metrics import HealthServer, WorkerHealth, WorkerMetrics
from storage.s3_storage import S3Storage


DEFAULT_INITIAL_STAGGER_SEED = "skaping-benchmark-v1"
logger = logging.getLogger(__name__)


def run_worker(
    *,
    max_jobs: int | None = None,
    epochs: int | None = None,
    run_for_seconds: float | None = None,
    initial_stagger_seed: str | None = None,
    dry_run: bool = False,
    stop_event: threading.Event | None = None,
    verbose: bool = False,
) -> list[dict[str, object]]:
    if epochs is not None and epochs < 1:
        raise ValueError("epochs must be positive")
    if run_for_seconds is not None and run_for_seconds <= 0:
        raise ValueError("run_for_seconds must be positive")
    if epochs is not None and run_for_seconds is not None:
        raise ValueError("epochs and run_for_seconds are mutually exclusive")
    worker = WorkerConfig.from_environment()
    skaping = SkapingIngestionConfig.from_environment()
    maximum = max_jobs or worker.max_jobs_per_epoch
    stop = stop_event or threading.Event()
    metrics = WorkerMetrics(source_network="ska")
    health = WorkerHealth(readiness_window_s=worker.readiness_window_s)
    server = HealthServer(worker.health_host, worker.health_port, health, metrics)
    server.start()
    gate = RateGate(skaping.request_delay_s)
    stagger = (
        InitialPollingStagger(initial_stagger_seed, worker.initial_stagger_window_s)
        if initial_stagger_seed is not None
        else None
    )
    summaries: list[dict[str, object]] = []
    deadline = (
        time.monotonic() + run_for_seconds
        if run_for_seconds is not None
        else None
    )
    try:
        while (
            not stop.is_set()
            and (epochs is None or len(summaries) < epochs)
            and (deadline is None or time.monotonic() < deadline)
        ):
            started = time.monotonic()
            try:
                summary = _run_epoch(
                    maximum,
                    dry_run,
                    worker,
                    skaping,
                    gate,
                    stop,
                    metrics,
                    len(summaries) + 1,
                    verbose,
                    stagger,
                )
                duration_s = time.monotonic() - started
                health.last_epoch_success_monotonic = time.monotonic()
                metrics.epochs.labels("ska", "success").inc()
                metrics.epoch_duration.labels("ska").observe(duration_s)
                summary["duration_s"] = round(duration_s, 6)
                summaries.append(summary)
                if epochs is None or len(summaries) < epochs:
                    delay = _epoch_wait_s(
                        duration_s,
                        worker.minimum_epoch_period_s,
                        worker.idle_delay_s,
                    )
                    if deadline is not None:
                        delay = min(
                            delay, max(0.0, deadline - time.monotonic())
                        )
                    stop.wait(delay)
            except Exception as error:
                logger.error(
                    "Skaping ingestion epoch failed: %s", type(error).__name__
                )
                metrics.epochs.labels("ska", "failure").inc()
                metrics.epoch_duration.labels("ska").observe(
                    time.monotonic() - started
                )
                if epochs is not None:
                    raise
                stop.wait(worker.failure_backoff_s)
    finally:
        health.intake_enabled = False
        server.close()
    return summaries


def _run_epoch(
    max_jobs: int,
    dry_run: bool,
    worker: WorkerConfig,
    skaping: SkapingIngestionConfig,
    gate: RateGate,
    stop: threading.Event,
    metrics: WorkerMetrics,
    epoch_number: int,
    verbose: bool,
    stagger: InitialPollingStagger | None,
) -> dict[str, object]:
    epoch_started = time.monotonic()
    pool = DatabaseConnectionPool(
        DatabaseConfig.from_environment(),
        worker.database_pool_size,
        metrics.observe_stage,
    )
    with ExitStack() as resources:
        resources.callback(pool.close)
        client = resources.enter_context(
            _new_client(skaping, observer=metrics.observe_stage, request_gate=gate)
        )
        storage = (
            S3Storage(
                S3Config.from_environment(),
                observer=metrics.observe_stage,
                event_observer=metrics.observe_event,
            )
            if not dry_run
            else None
        )
        publisher = (
            resources.enter_context(
                MqttPublisher(
                    MqttConfig.from_environment(),
                    observer=metrics.observe_stage,
                    event_observer=metrics.observe_event,
                )
            )
            if not dry_run
            else None
        )
        with pool.connection() as connection:
            jobs = get_due_source_streams(
                connection,
                "ska",
                timedelta(seconds=skaping.minimum_ingestion_interval_s),
                polling_interval_factor=skaping.polling_interval_factor,
                minimum_polling_interval=timedelta(
                    seconds=skaping.minimum_polling_interval_s
                ),
                limit=max_jobs,
            )
            connection.commit()
        deferred = 0
        initial_release_ids: set[str] = set()
        if stagger is not None:
            jobs, deferred, initial_release_ids = stagger.select(jobs)
        metrics.selected.labels("ska").set(len(jobs))
        if not jobs or stop.is_set():
            return {
                "selected": len(jobs),
                "outcomes": {},
                "stagger_deferred": deferred,
            }
        results = []
        with ThreadPoolExecutor(
            max_workers=worker.threads, thread_name_prefix="skaping"
        ) as executor:
            futures: dict[Future, object] = {
                executor.submit(
                    _process_due_job,
                    job,
                    dry_run,
                    skaping,
                    client,
                    storage,
                    publisher,
                    metrics,
                ): job
                for job in jobs
            }
            for future in as_completed(futures):
                job = futures[future]
                result = _completed_future_result(
                    future,
                    job,
                    network_id="ska",
                    epoch_number=epoch_number,
                    metrics=metrics,
                )
                results.append(result)
                if verbose:
                    print(
                        json.dumps(
                            _progress(epoch_number, len(jobs), results),
                            sort_keys=True,
                        ),
                        flush=True,
                    )
        state_updates = [
            result.state_update
            for result in results
            if getattr(result, "state_update", None) is not None
        ]
        state_updates = [
            replace(update, ema_update_candidate=None)
            if update.source_stream_id in initial_release_ids
            else update
            for update in state_updates
        ]
        candidates = [
            result.ema_update_candidate
            for result in results
            if isinstance(result.ema_update_candidate, EmaUpdateCandidate)
            and result.ema_update_candidate.source_stream_id not in initial_release_ids
        ]
        ema_update_eligible = not dry_run and _ema_update_allowed(
            epoch_number,
            time.monotonic() - epoch_started,
            skaping.minimum_ingestion_interval_s,
        )
        applied = 0
        if not dry_run and state_updates:
            with pool.connection() as connection:
                applied = apply_ingestion_state_updates(
                    connection, state_updates, apply_ema=ema_update_eligible
                )
                connection.commit()
            if ema_update_eligible:
                metrics.observe_period_estimate_updates(candidates)
    return {
        "selected": len(jobs),
        "outcomes": dict(
            sorted(Counter(result.outcome for result in results).items())
        ),
        "ema_candidates": len(candidates),
        "state_updates_applied": applied,
        "ema_updates_applied": len(candidates) if ema_update_eligible else 0,
        "direct_period_replacements_applied": (
            sum(
                candidate.update_method == "direct_replacement"
                for candidate in candidates
            )
            if ema_update_eligible
            else 0
        ),
        "stagger_deferred": deferred,
    }


def _process_due_job(job, dry_run, skaping, client, storage, publisher, metrics):
    return _process_job(
        client,
        job,
        dry_run=dry_run,
        ema_alpha=skaping.ema_alpha,
        direct_replacement_modulus=skaping.period_direct_replacement_modulus,
        transformation=TransformationConfig.from_environment(),
        storage=storage,
        publisher=publisher,
        stage_observer=metrics.observe_stage,
        event_observer=metrics.observe_event,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-jobs", type=int)
    limits = parser.add_mutually_exclusive_group()
    limits.add_argument("--epochs", type=int)
    limits.add_argument("--run-for-seconds", type=float)
    parser.add_argument("--stagger-initial-polling", action="store_true")
    parser.add_argument("--stagger-seed", default=DEFAULT_INITIAL_STAGGER_SEED)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    stop = threading.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: stop.set())
    summaries = run_worker(
        max_jobs=args.max_jobs,
        epochs=args.epochs,
        run_for_seconds=args.run_for_seconds,
        initial_stagger_seed=(
            args.stagger_seed if args.stagger_initial_polling else None
        ),
        dry_run=args.dry_run,
        stop_event=stop,
        verbose=args.verbose,
    )
    print(json.dumps({"worker_summaries": summaries}, sort_keys=True))


if __name__ == "__main__":
    main()
