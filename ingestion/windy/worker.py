"""Continuous Windy ingestion worker with bounded validation controls."""

from __future__ import annotations

import argparse
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from contextlib import ExitStack
from dataclasses import asdict, replace
from datetime import timedelta
import json
import logging
import signal
import threading
import time

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
    PeriodEstimateCandidate,
    apply_ingestion_state_updates,
    get_due_source_streams,
    reset_network_period_estimates,
)
from ingestion.notification.mqtt_publisher import MqttPublisher
from ingestion.shared.source_processing import process_job
from ingestion.shared.worker_support import (
    DatabaseConnectionPool,
    InitialPollingStagger,
    RateGate,
    completed_future_result,
    connect_database,
    epoch_wait_s,
    period_update_allowed,
    progress,
)
from ingestion.windy.windy_image_access import WindyImageClient
from ingestion.windy.windy_ingestion_workflow import _validate_countries
from ingestion.shared.worker_metrics import HealthServer, WorkerHealth, WorkerMetrics
from storage.s3_storage import S3Storage


logger = logging.getLogger(__name__)
DEFAULT_INITIAL_STAGGER_SEED = "windy-benchmark-v1"


def run_worker(
    *,
    network: str,
    countries: tuple[str, ...],
    max_jobs: int | None = None,
    epochs: int | None = None,
    run_for_seconds: float | None = None,
    initial_stagger_seed: str | None = None,
    batch_freshness: bool = False,
    dry_run: bool = False,
    stop_event: threading.Event | None = None,
    verbose: bool = False,
    reset_windy_period_estimates: bool = False,
) -> list[dict[str, object]]:
    if network != "win":
        raise ValueError("checkpoint 7 supports only network 'win'")
    if epochs is not None and epochs < 1:
        raise ValueError("epochs must be positive")
    if run_for_seconds is not None and run_for_seconds <= 0:
        raise ValueError("run_for_seconds must be positive")
    if epochs is not None and run_for_seconds is not None:
        raise ValueError("epochs and run_for_seconds are mutually exclusive")
    normalized = _validate_countries(countries)
    worker = WorkerConfig.from_environment()
    windy = WindyIngestionConfig.from_environment()
    if reset_windy_period_estimates:
        with connect_database(DatabaseConfig.from_environment()) as connection:
            reset_count = reset_network_period_estimates(connection, "win")
        print(
            json.dumps(
                {
                    "windy_period_estimate_reset": {
                        "reset_streams": reset_count,
                        "provider_timestamps_preserved": True,
                        "download_timestamps_preserved": True,
                        "processed_timestamps_preserved": True,
                    }
                },
                sort_keys=True,
            ),
            flush=True,
        )
    maximum = max_jobs or worker.max_jobs_per_epoch
    stop = stop_event or threading.Event()
    metrics = WorkerMetrics()
    health = WorkerHealth(readiness_window_s=worker.readiness_window_s)
    server = HealthServer(worker.health_host, worker.health_port, health, metrics)
    server.start()
    gate = RateGate(windy.request_delay_s)
    initial_stagger = (
        InitialPollingStagger(initial_stagger_seed, worker.initial_stagger_window_s)
        if initial_stagger_seed is not None
        else None
    )
    summaries: list[dict[str, object]] = []
    epoch_index = 0
    deadline = (
        time.monotonic() + run_for_seconds
        if run_for_seconds is not None
        else None
    )
    try:
        while (
            not stop.is_set()
            and (epochs is None or epoch_index < epochs)
            and (deadline is None or time.monotonic() < deadline)
        ):
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
                    initial_stagger,
                    batch_freshness,
                )
                health.last_epoch_success_monotonic = time.monotonic()
                metrics.epochs.labels("win", "success").inc()
                duration_s = time.monotonic() - started
                summary["duration_s"] = round(duration_s, 6)
                summaries.append(summary)
                epoch_index += 1
                metrics.epoch_duration.labels("win").observe(duration_s)
                if epochs is None or epoch_index < epochs:
                    wait_s = epoch_wait_s(
                        time.monotonic() - started,
                        worker.minimum_epoch_period_s,
                        worker.idle_delay_s,
                    )
                    if deadline is not None:
                        wait_s = min(wait_s, max(0.0, deadline - time.monotonic()))
                    stop.wait(wait_s)
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
    initial_stagger: InitialPollingStagger | None = None,
    batch_freshness: bool = False,
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
                freshness_query_retry_count=windy.freshness_query_retry_count,
                download_retry_count=windy.download_retry_count,
                retry_backoff_s=windy.retry_backoff_s,
                request_gate=gate,
                observer=metrics.observe_stage,
            )
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
                "win",
                timedelta(seconds=windy.minimum_ingestion_interval_s),
                polling_interval_factor=windy.polling_interval_factor,
                minimum_polling_interval=timedelta(
                    seconds=windy.minimum_polling_interval_s
                ),
                countries=countries,
                limit=max_jobs,
            )
            connection.commit()
        stagger_deferred = 0
        initial_release_ids: set[str] = set()
        if initial_stagger is not None:
            jobs, stagger_deferred, initial_release_ids = initial_stagger.select(jobs)
        metrics.selected.labels("win").set(len(jobs))
        if not jobs or stop.is_set():
            summary: dict[str, object] = {"selected": len(jobs), "outcomes": {}}
            if initial_stagger is not None:
                summary["stagger_deferred"] = stagger_deferred
            return summary
        batch_summary: dict[str, int] | None = None
        if batch_freshness:
            batch_result = client.refresh(
                [
                    (job.provider_source_stream_id, job.selected_rendition)
                    for job in jobs
                ],
                max_workers=worker.threads,
            )
            batch_summary = asdict(batch_result)
            metrics.observe_event(
                "freshness_batch",
                {
                    **batch_summary,
                    "batch_size": 50,
                },
            )
        results = []
        with ThreadPoolExecutor(max_workers=worker.threads, thread_name_prefix="windy") as executor:
            futures: dict[Future, DueSourceStream] = {
                executor.submit(
                    _process_due_job,
                    job,
                    dry_run,
                    windy,
                    client,
                    storage,
                    publisher,
                    metrics.observe_stage,
                    metrics.observe_event,
                ): job
                for job in jobs
            }
            last_report = 0.0
            while futures:
                done, _ = wait(futures, timeout=1, return_when=FIRST_COMPLETED)
                for future in done:
                    job = futures.pop(future)
                    result = completed_future_result(
                        future,
                        job,
                        network_id="win",
                        epoch_number=epoch_number,
                        metrics=metrics,
                    )
                    results.append(result)
                now = time.monotonic()
                if verbose and (now - last_report >= 1 or not futures):
                    print(json.dumps(progress(epoch_number, len(jobs), results), sort_keys=True), flush=True)
                    last_report = now
        state_updates = [
            result.state_update
            for result in results
            if getattr(result, "state_update", None) is not None
        ]
        state_updates = [
            replace(update, period_estimate_candidate=None)
            if update.source_stream_id in initial_release_ids
            else update
            for update in state_updates
        ]
        candidates = [
            result.period_estimate_candidate
            for result in results
            if isinstance(result.period_estimate_candidate, PeriodEstimateCandidate)
        ]
        applicable_candidates = [
            candidate
            for candidate in candidates
            if candidate.source_stream_id not in initial_release_ids
        ]
        state_updates_applied = 0
        period_update_eligible = not dry_run and period_update_allowed(
            epoch_number,
            time.monotonic() - epoch_started,
            windy.minimum_ingestion_interval_s,
        )
        if not dry_run and state_updates:
            with pool.connection() as connection:
                state_updates_applied = apply_ingestion_state_updates(
                    connection,
                    state_updates,
                    apply_period_estimate=period_update_eligible,
                )
                connection.commit()
            if period_update_eligible:
                metrics.observe_period_estimate_updates(applicable_candidates)
    outcomes: dict[str, int] = {}
    for result in results:
        outcomes[result.outcome] = outcomes.get(result.outcome, 0) + 1
    summary = {
        "selected": len(jobs),
        "outcomes": dict(sorted(outcomes.items())),
        "period_candidates": len(candidates),
        "state_updates_applied": state_updates_applied,
        "period_updates_applied": (
            len(applicable_candidates) if period_update_eligible else 0
        ),
        "direct_period_replacements_applied": (
            sum(
                candidate.update_method == "direct_replacement"
                for candidate in applicable_candidates
            )
            if period_update_eligible
            else 0
        ),
    }
    if batch_summary is not None:
        summary["freshness_batch"] = batch_summary
    if initial_stagger is not None:
        summary["stagger_deferred"] = stagger_deferred
        summary["period_candidates_deferred_initial"] = len(candidates) - len(
            applicable_candidates
        )
    return summary


def _process_due_job(
    job: DueSourceStream,
    dry_run: bool,
    windy: WindyIngestionConfig,
    client: WindyImageClient,
    storage: S3Storage | None,
    publisher: MqttPublisher | None,
    stage_observer,
    event_observer,
):
    return process_job(
        client,
        job,
        dry_run=dry_run,
        minimum_period_seconds=windy.minimum_ingestion_interval_s,
        direct_replacement_modulus=windy.period_direct_replacement_modulus,
        transformation=TransformationConfig.from_environment(),
        storage=storage,
        publisher=publisher,
        stage_observer=stage_observer,
        event_observer=event_observer,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--network", default="win")
    parser.add_argument(
        "--countries", default=",".join(EUMETNET_MEMBER_COUNTRIES)
    )
    parser.add_argument("--max-jobs", type=int)
    limit = parser.add_mutually_exclusive_group()
    limit.add_argument("--epochs", type=int)
    limit.add_argument(
        "--run-for-seconds",
        type=float,
        help="stop starting epochs after this duration; finish an active epoch",
    )
    parser.add_argument(
        "--stagger-initial-polling",
        action="store_true",
        help="deterministically spread each stream's first check",
    )
    parser.add_argument(
        "--stagger-seed",
        default=DEFAULT_INITIAL_STAGGER_SEED,
        help="fixed seed for deterministic initial polling phases",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--batch-freshness",
        action="store_true",
        help="resolve due Windy streams using listings of up to 50 explicit IDs",
    )
    parser.add_argument(
        "--verbose", action="store_true", help="print epoch progress once per second"
    )
    parser.add_argument(
        "--reset-windy-period-estimates",
        action="store_true",
        help="clear only Windy period estimates before the worker starts",
    )
    args = parser.parse_args()
    stop = threading.Event()

    def request_stop(*_: object) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    initial_stagger_seed = args.stagger_seed if args.stagger_initial_polling else None
    if initial_stagger_seed is not None:
        print(
            json.dumps(
                {
                    "initial_polling_stagger": {
                        "enabled": True,
                        "seed": initial_stagger_seed,
                        "window_s": WorkerConfig.from_environment().initial_stagger_window_s,
                    }
                },
                sort_keys=True,
            ),
            flush=True,
        )
    summaries = run_worker(
        network=args.network,
        countries=tuple(args.countries.split(",")),
        max_jobs=args.max_jobs,
        epochs=args.epochs,
        run_for_seconds=args.run_for_seconds,
        initial_stagger_seed=initial_stagger_seed,
        batch_freshness=args.batch_freshness,
        dry_run=args.dry_run,
        stop_event=stop,
        verbose=args.verbose,
        reset_windy_period_estimates=args.reset_windy_period_estimates,
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
