from unittest.mock import Mock

from prometheus_client import generate_latest

from config.deployment_config import MaintenanceMetricsConfig
from observability.maintenance_metrics import MaintenanceJobMetrics


def config(*, enabled: bool = False) -> MaintenanceMetricsConfig:
    return MaintenanceMetricsConfig(enabled, "http://pushgateway:9091", 5)


def test_maintenance_metrics_use_unambiguous_names() -> None:
    metrics = MaintenanceJobMetrics("spool_cleanup", config())

    pushed = metrics.publish(
        success=True,
        duration_s=2.5,
        items={"deleted": 3},
        bytes_by_outcome={"deleted": 1200},
        stages={"listing": 0.5},
        retention_hours=24,
    )

    assert pushed is False
    events = generate_latest(metrics.events)
    state = generate_latest(metrics.state)
    assert b"webcam_maintenance_run_total" in events
    assert b"webcam_maintenance_items_total" in events
    assert b"webcam_maintenance_bytes_total" in events
    assert b"webcam_maintenance_stage_duration_seconds" in events
    assert b"webcam_maintenance_last_success_unixtime" in state
    assert b"webcam_batch_" not in events + state


def test_pushgateway_jobs_and_grouping_use_maintenance_names(monkeypatch) -> None:
    push_events = Mock()
    push_state = Mock()
    monkeypatch.setattr(
        "observability.maintenance_metrics.pushadd_to_gateway", push_events
    )
    monkeypatch.setattr(
        "observability.maintenance_metrics.push_to_gateway", push_state
    )
    metrics = MaintenanceJobMetrics("database_backup", config(enabled=True))

    assert metrics.publish(
        success=True,
        duration_s=4,
        backup_size_bytes=2048,
    )

    assert push_events.call_args.kwargs["job"] == "webcam_maintenance_events"
    assert push_events.call_args.kwargs["grouping_key"] == {
        "maintenance_job": "database_backup"
    }
    assert push_state.call_args.kwargs["job"] == "webcam_maintenance_state"
    assert push_state.call_args.kwargs["grouping_key"] == {
        "maintenance_job": "database_backup"
    }
