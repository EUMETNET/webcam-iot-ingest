from threading import Event
from unittest.mock import Mock

from config.deployment_config import WindyIngestionConfig, WorkerConfig
from ingestion.worker import (
    DatabaseConnectionPool,
    LazyPooledConnection,
    _epoch_wait_s,
    _progress,
    _run_epoch,
)
from ingestion.worker_metrics import WorkerMetrics
from tests.ingestion.windy.test_windy_ingestion_workflow import job


def test_epoch_respects_limit_and_uses_bounded_pool(monkeypatch) -> None:
    jobs = [job(), job()]
    jobs[1] = jobs[1].__class__(**{**jobs[1].__dict__, "source_stream_id": "win43"})
    connection = Mock(closed=False)
    monkeypatch.setattr("ingestion.worker._connect", lambda config: connection)
    monkeypatch.setattr("ingestion.worker.get_due_source_streams", lambda *a, **k: jobs)
    result = Mock(outcome="downloaded")
    monkeypatch.setattr("ingestion.worker._process_due_job", lambda *a, **k: result)
    worker = WorkerConfig(2, 2, 0, 0, 1, "127.0.0.1", 0, 10)
    windy = WindyIngestionConfig.from_environment()

    summary = _run_epoch(
        ("DK",), 2, True, worker, windy, lambda: None, Event(), WorkerMetrics()
    )

    assert summary == {
        "selected": 2,
        "outcomes": {"downloaded": 2},
        "ema_candidates": 0,
        "ema_updates_applied": 0,
    }
    connection.close.assert_called_once()


def test_stopped_epoch_does_not_start_jobs(monkeypatch) -> None:
    connection = Mock(closed=False)
    monkeypatch.setattr("ingestion.worker._connect", lambda config: connection)
    monkeypatch.setattr("ingestion.worker.get_due_source_streams", lambda *a, **k: [job()])
    process = Mock()
    monkeypatch.setattr("ingestion.worker._process_due_job", process)
    stop = Event()
    stop.set()

    summary = _run_epoch(
        ("DK",), 1, True,
        WorkerConfig(1, 1, 0, 0, 1, "127.0.0.1", 0, 10),
        WindyIngestionConfig.from_environment(), lambda: None, stop, WorkerMetrics(),
    )

    assert summary == {"selected": 1, "outcomes": {}}
    process.assert_not_called()


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


def test_lazy_connection_is_not_borrowed_until_database_use(monkeypatch) -> None:
    connection = Mock(closed=False)
    connector = Mock(return_value=connection)
    monkeypatch.setattr("ingestion.worker._connect", connector)
    pool = DatabaseConnectionPool(Mock(), 1)

    with LazyPooledConnection(pool):
        connector.assert_not_called()
    with LazyPooledConnection(pool) as lazy:
        lazy.commit()
    with LazyPooledConnection(pool) as lazy:
        lazy.rollback()

    connector.assert_called_once()
    connection.commit.assert_called_once()
    connection.rollback.assert_called_once()
    pool.close()
    connection.close.assert_called_once()


def test_epoch_wait_enforces_period_and_idle_floor() -> None:
    assert _epoch_wait_s(4, 15, 5) == 11
    assert _epoch_wait_s(14, 15, 5) == 5
    assert _epoch_wait_s(20, 15, 5) == 5
