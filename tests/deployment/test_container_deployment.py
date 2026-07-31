"""Structural checks for the container-first pilot deployment."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[2]


def _compose_config() -> dict[str, object]:
    if shutil.which("docker") is None:
        pytest.skip("Docker Compose is unavailable")
    completed = subprocess.run(
        [
            "docker",
            "compose",
            "--env-file",
            ".env.example",
            "--profile",
            "application",
            "--profile",
            "monitoring",
            "--profile",
            "jobs",
            "config",
            "--format",
            "json",
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout)


def test_long_lived_workers_have_isolated_runtime_controls() -> None:
    services = _compose_config()["services"]
    expected_ports = {
        "windy-worker": "8002",
        "fintraffic-worker": "8003",
        "skaping-worker": "8004",
    }
    for service_name, port in expected_ports.items():
        service = services[service_name]
        assert service["restart"] == "unless-stopped"
        assert service["stop_grace_period"] == "30s"
        assert service["cpus"] > 0
        assert service["mem_limit"]
        assert service["environment"]["INGESTION_HEALTH_PORT"] == port
        assert service["healthcheck"]["test"]
        assert (
            service["depends_on"]["schema-migrate"]["condition"]
            == "service_completed_successfully"
        )
        assert service["depends_on"]["mqtt"]["condition"] == "service_healthy"


def test_persistent_state_and_short_lived_job_are_declared() -> None:
    config = _compose_config()
    assert {
        "postgres-data",
        "prometheus-data",
        "grafana-storage",
        "pushgateway-data",
    } <= set(config["volumes"])
    job = config["services"]["webcam-job"]
    assert job["depends_on"]["schema-migrate"]["condition"] == (
        "service_completed_successfully"
    )
    assert job["environment"]["DISCOVERY_METRICS_GATEWAY_URL"] == (
        "http://pushgateway:9091"
    )
    assert job["environment"]["BATCH_METRICS_GATEWAY_URL"] == (
        "http://pushgateway:9091"
    )
    assert job["environment"]["MQTT_HOST"] == "mqtt"
    assert {secret["source"] for secret in job["secrets"]} >= {
        "database_password",
        "s3_access_key",
        "s3_secret_key",
    }


def test_systemd_only_orchestrates_compose_and_scheduled_jobs() -> None:
    directory = ROOT / "deployment/systemd/pilot"
    stack = (directory / "webcam-stack.service").read_text()
    discovery_timer = (directory / "webcam-discovery.timer").read_text()
    maintenance_timer = (directory / "webcam-maintenance.timer").read_text()
    maintenance = (directory / "run-maintenance-sequence").read_text()

    assert "--profile application --profile monitoring up -d" in stack
    assert "python -m ingestion" not in stack
    assert "OnCalendar=*-*-* 12:00:00 UTC" in discovery_timer
    assert "OnCalendar=*-*-* 01:00:00 UTC" in maintenance_timer
    assert maintenance.index("webcam-db-backup.service") < maintenance.index(
        "webcam-spool-cleanup.service"
    )


def test_operational_just_recipes_use_container_jobs() -> None:
    justfile = (ROOT / "justfile").read_text()
    operational = justfile.split(
        "# One dry-run discovery used by the accelerated checkpoint-12"
    )[0]

    assert "uv run" not in operational
    assert "container-discover windy" in operational
    assert "container-discover fintraffic" in operational
    assert "container-discover skaping" in operational
    assert "container-cleanup-spool" in operational
    assert "container-backup-database" in operational
    assert "ingestion.windy.windy_ingestion_workflow" in justfile


def test_checkpoint13_quiet_baseline_has_one_delayed_sequential_workflow() -> None:
    directory = ROOT / "deployment/systemd/checkpoint13-30min-baseline"
    workflow = (directory / "run-workflow").read_text()
    workflow_timer = (
        directory / "webcam-checkpoint13-baseline-workflow.timer"
    ).read_text()
    workflow_service = (
        directory / "webcam-checkpoint13-baseline-workflow.service"
    ).read_text()
    stop_timer = (
        directory / "webcam-checkpoint13-baseline-stop.timer"
    ).read_text()
    stack = (
        directory / "webcam-checkpoint13-baseline-stack.service"
    ).read_text()

    expected = [
        "discovery@${network}.service",
        "baseline-db-backup.service",
        "baseline-spool-cleanup.service",
    ]
    positions = [workflow.index(value) for value in expected]
    assert positions == sorted(positions)
    assert "OnActiveSec=10min" in workflow_timer
    assert "OnActiveSec=30min" in stop_timer
    assert "PartOf=webcam-checkpoint13-baseline.target" not in workflow_service
    assert "--profile application up -d --build" in stack
    assert "stop windy-worker fintraffic-worker skaping-worker" in stack
