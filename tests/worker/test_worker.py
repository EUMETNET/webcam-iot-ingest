from threading import Event
from unittest.mock import Mock

import pytest
from psycopg.pq import TransactionStatus

from config.deployment_config import WindyIngestionConfig, WorkerConfig
from ingestion.shared.worker_support import (
    DatabaseConnectionPool,
    InitialPollingStagger,
    _initial_phase_s,
    completed_future_result as _completed_future_result,
    epoch_wait_s as _epoch_wait_s,
    period_update_allowed as _period_update_allowed,
    progress as _progress,
)
from ingestion.windy.worker import (
    DEFAULT_INITIAL_STAGGER_SEED,
    _run_epoch,
    run_worker,
)
from ingestion.shared.worker_metrics import WorkerMetrics
from ingestion.windy.windy_image_access import WindyBatchFreshnessResult
from tests.ingestion.windy.test_windy_ingestion_workflow import job


def test_epoch_respects_limit_and_uses_bounded_pool(monkeypatch) -> None:
    jobs = [job(), job()]
    jobs[1] = jobs[1].__class__(**{**jobs[1].__dict__, "source_stream_id": "win43"})
    connection = Mock(closed=False)
    connection.info.transaction_status = TransactionStatus.IDLE
    monkeypatch.setattr("ingestion.shared.worker_support.connect_database", lambda config: connection)
    monkeypatch.setattr("ingestion.windy.worker.get_due_source_streams", lambda *a, **k: jobs)
    result = Mock(outcome="downloaded")
    monkeypatch.setattr("ingestion.windy.worker._process_due_job", lambda *a, **k: result)
    worker = WorkerConfig(2, 2, 0, 0, 1, "127.0.0.1", 0, 10)
    windy = WindyIngestionConfig.from_environment()

    summary = _run_epoch(
        ("DK",), 2, True, worker, windy, lambda: None, Event(), WorkerMetrics()
    )

    assert summary == {
        "selected": 2,
        "outcomes": {"downloaded": 2},
        "period_candidates": 0,
        "state_updates_applied": 0,
        "period_updates_applied": 0,
        "direct_period_replacements_applied": 0,
    }
    connection.close.assert_called_once()


def test_stopped_epoch_does_not_start_jobs(monkeypatch) -> None:
    connection = Mock(closed=False)
    connection.info.transaction_status = TransactionStatus.IDLE
    monkeypatch.setattr("ingestion.shared.worker_support.connect_database", lambda config: connection)
    monkeypatch.setattr("ingestion.windy.worker.get_due_source_streams", lambda *a, **k: [job()])
    process = Mock()
    monkeypatch.setattr("ingestion.windy.worker._process_due_job", process)
    stop = Event()
    stop.set()

    summary = _run_epoch(
        ("DK",), 1, True,
        WorkerConfig(1, 1, 0, 0, 1, "127.0.0.1", 0, 10),
        WindyIngestionConfig.from_environment(), lambda: None, stop, WorkerMetrics(),
    )

    assert summary == {"selected": 1, "outcomes": {}}
    process.assert_not_called()


def test_epoch_can_prime_batched_freshness_before_jobs(monkeypatch) -> None:
    selected = job()
    connection = Mock(closed=False)
    connection.info.transaction_status = TransactionStatus.IDLE
    monkeypatch.setattr("ingestion.shared.worker_support.connect_database", lambda config: connection)
    monkeypatch.setattr(
        "ingestion.windy.worker.get_due_source_streams", lambda *a, **k: [selected]
    )
    result = Mock(outcome="unchanged", period_estimate_candidate=None)
    monkeypatch.setattr(
        "ingestion.windy.worker._process_due_job", lambda *a, **k: result
    )
    refresh = Mock(
        return_value=WindyBatchFreshnessResult(
            requested_streams=1,
            returned_streams=1,
            missing_streams=0,
            successful_requests=1,
            failed_requests=0,
            throttled_requests=0,
        )
    )
    monkeypatch.setattr(
        "ingestion.windy.worker.WindyImageClient.refresh", refresh
    )

    summary = _run_epoch(
        ("DK",),
        1,
        True,
        WorkerConfig(2, 1, 0, 0, 1, "127.0.0.1", 0, 10),
        WindyIngestionConfig.from_environment(),
        lambda: None,
        Event(),
        WorkerMetrics(),
        batch_freshness=True,
    )

    refresh.assert_called_once_with(
        [(selected.provider_source_stream_id, selected.selected_rendition)],
        max_workers=2,
    )
    assert summary["freshness_batch"] == {
        "requested_streams": 1,
        "returned_streams": 1,
        "missing_streams": 0,
        "successful_requests": 1,
        "failed_requests": 0,
        "throttled_requests": 0,
    }


def test_verbose_progress_counts_download_upload_and_throttling() -> None:
    results = [
        Mock(outcome="published"),
        Mock(outcome="mqtt_error"),
        Mock(outcome="storage_error"),
        Mock(outcome="unchanged"),
        Mock(outcome="throttled"),
    ]

    assert _progress(3, 10, results) == {
        "worker_progress": {
            "epoch": 3,
            "position": "5/10",
            "completed": 5,
            "selected": 10,
            "downloaded": 3,
            "uploaded": 2,
            "throttled": 1,
        }
    }


@pytest.mark.parametrize(
    "initial_status",
    [TransactionStatus.INTRANS, TransactionStatus.INERROR],
)
def test_pool_rolls_back_non_idle_connection(monkeypatch, initial_status) -> None:
    connection = Mock(closed=False)
    connection.info.transaction_status = initial_status

    def rollback() -> None:
        connection.info.transaction_status = TransactionStatus.IDLE

    connection.rollback.side_effect = rollback
    connector = Mock(return_value=connection)
    monkeypatch.setattr("ingestion.shared.worker_support.connect_database", connector)
    pool = DatabaseConnectionPool(Mock(), 1)

    with pool.connection():
        pass
    with pool.connection() as reused:
        assert reused is connection

    connector.assert_called_once()
    connection.rollback.assert_called_once()
    pool.close()


def test_pool_returns_idle_connection_without_rollback(monkeypatch) -> None:
    connection = Mock(closed=False)
    connection.info.transaction_status = TransactionStatus.IDLE
    connector = Mock(return_value=connection)
    monkeypatch.setattr("ingestion.shared.worker_support.connect_database", connector)
    pool = DatabaseConnectionPool(Mock(), 1)

    with pool.connection():
        pass
    with pool.connection() as reused:
        assert reused is connection

    connection.rollback.assert_not_called()
    connector.assert_called_once()
    pool.close()


def test_pool_replaces_connection_closed_by_borrower(monkeypatch) -> None:
    closed = Mock(closed=False)
    closed.info.transaction_status = TransactionStatus.IDLE
    replacement = Mock(closed=False)
    replacement.info.transaction_status = TransactionStatus.IDLE
    connector = Mock(side_effect=[closed, replacement])
    monkeypatch.setattr("ingestion.shared.worker_support.connect_database", connector)
    pool = DatabaseConnectionPool(Mock(), 1)

    with pool.connection():
        closed.closed = True
    with pool.connection() as connection:
        assert connection is replacement

    assert connector.call_count == 2
    pool.close()


def test_pool_discards_connection_when_rollback_fails(monkeypatch) -> None:
    broken = Mock(closed=False)
    broken.info.transaction_status = TransactionStatus.INERROR
    broken.rollback.side_effect = RuntimeError("rollback failed")
    replacement = Mock(closed=False)
    replacement.info.transaction_status = TransactionStatus.IDLE
    connector = Mock(side_effect=[broken, replacement])
    monkeypatch.setattr("ingestion.shared.worker_support.connect_database", connector)
    pool = DatabaseConnectionPool(Mock(), 1)

    with pool.connection() as connection:
        assert connection is broken
    with pool.connection() as connection:
        assert connection is replacement

    broken.close.assert_called_once()
    assert connector.call_count == 2
    pool.close()


def test_pool_observes_discard_and_replacement(monkeypatch) -> None:
    broken = Mock(closed=False)
    broken.info.transaction_status = TransactionStatus.INERROR
    broken.rollback.side_effect = RuntimeError("rollback failed")
    replacement = Mock(closed=False)
    replacement.info.transaction_status = TransactionStatus.IDLE
    monkeypatch.setattr(
        "ingestion.shared.worker_support.connect_database", Mock(side_effect=[broken, replacement])
    )
    events = []
    pool = DatabaseConnectionPool(
        Mock(), 1, lambda stage, outcome, duration: events.append((stage, outcome))
    )

    with pool.connection():
        pass
    with pool.connection():
        pass

    assert ("database_pool_cleanup", "discard") in events
    assert ("database_pool_cleanup", "replacement") in events
    pool.close()


def test_unexpected_future_error_is_accounted_without_raising(caplog) -> None:
    future = Mock()
    future.result.side_effect = RuntimeError("unexpected")
    metrics = WorkerMetrics()
    stream = job()

    result = _completed_future_result(
        future,
        stream,
        network_id="win",
        epoch_number=7,
        metrics=metrics,
    )

    assert result.outcome == "internal_error"
    assert result.source_stream_id == stream.source_stream_id
    assert "source_stream_id=win42 network=win epoch=7" in caplog.text


def test_epoch_wait_enforces_period_and_idle_floor() -> None:
    assert _epoch_wait_s(4, 15, 5) == 11
    assert _epoch_wait_s(14, 15, 5) == 5
    assert _epoch_wait_s(20, 15, 5) == 5


def test_period_update_keeps_first_epoch_and_overlong_epoch_guards() -> None:
    assert not _period_update_allowed(1, 10, 300)
    assert _period_update_allowed(2, 299.999, 300)
    assert not _period_update_allowed(2, 300, 300)
    assert not _period_update_allowed(9, 301, 300)


def test_worker_rejects_conflicting_or_invalid_time_limits() -> None:
    with pytest.raises(ValueError, match="mutually exclusive"):
        run_worker(network="win", countries=("DK",), epochs=1, run_for_seconds=60)
    with pytest.raises(ValueError, match="run_for_seconds must be positive"):
        run_worker(network="win", countries=("DK",), run_for_seconds=0)


def test_initial_stagger_is_deterministic_and_releases_without_db_changes() -> None:
    stream = job()
    windy = WindyIngestionConfig.from_environment()
    window_s = 600.0
    phase_s = _initial_phase_s(stream, DEFAULT_INITIAL_STAGGER_SEED, window_s)

    assert phase_s == _initial_phase_s(
        stream, DEFAULT_INITIAL_STAGGER_SEED, window_s
    )
    assert 0 <= phase_s < window_s

    stagger = InitialPollingStagger(DEFAULT_INITIAL_STAGGER_SEED, window_s, 100.0)
    selected, deferred, released = stagger.select(
        [stream], now_monotonic=100.0 + max(0.0, phase_s - 0.001)
    )
    assert selected == []
    assert deferred == 1
    assert released == set()

    selected, deferred, released = stagger.select(
        [stream], now_monotonic=100.0 + phase_s
    )
    assert selected == [stream]
    assert deferred == 0
    assert released == {stream.source_stream_id}
    assert stagger.select([stream], now_monotonic=100.0) == (
        [stream],
        0,
        set(),
    )
