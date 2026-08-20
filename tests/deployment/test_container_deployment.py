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


def test_prometheus_scrapes_production_workers_symmetrically() -> None:
    prometheus = (ROOT / "prometheus/prometheus.yml").read_text()
    production_workers = prometheus.split(
        "  - job_name: ingestion-workers", maxsplit=1
    )[1].split("  - job_name: windy-worker-host-benchmark", maxsplit=1)[0]

    assert 'targets: ["windy-worker:8002"]' in production_workers
    assert "source_network: win" in production_workers
    assert 'targets: ["fintraffic-worker:8003"]' in production_workers
    assert "source_network: fin" in production_workers
    assert 'targets: ["skaping-worker:8004"]' in production_workers
    assert "source_network: ska" in production_workers
    assert "  - job_name: windy-worker\n" not in prometheus
    assert "  - job_name: fintraffic-worker\n" not in prometheus
    assert "  - job_name: skaping-worker\n" not in prometheus

    alerts = (ROOT / "prometheus/alerts.yml").read_text()
    assert 'up{job="ingestion-workers"} == 0' in alerts
    assert "$labels.source_network" in alerts

    dashboard = json.loads(
        (ROOT / "grafana/dashboards/infrastructure-health.json").read_text()
    )
    scrape_health = next(
        panel for panel in dashboard["panels"] if panel["id"] == 1
    )
    expressions = [target["expr"] for target in scrape_health["targets"]]
    assert 'up{job="ingestion-workers"}' in expressions


def test_persistent_state_and_short_lived_job_are_declared() -> None:
    config = _compose_config()
    assert {
        "postgres-data",
        "prometheus-data",
        "grafana-storage",
        "pushgateway-data",
        "alertmanager-data",
    } <= set(config["volumes"])
    job = config["services"]["webcam-job"]
    assert job["depends_on"]["schema-migrate"]["condition"] == (
        "service_completed_successfully"
    )
    assert job["environment"]["DISCOVERY_METRICS_GATEWAY_URL"] == (
        "http://pushgateway:9091"
    )
    assert job["environment"]["MAINTENANCE_METRICS_GATEWAY_URL"] == (
        "http://pushgateway:9091"
    )
    assert job["environment"]["MQTT_HOST"] == "mqtt"
    assert {secret["source"] for secret in job["secrets"]} >= {
        "database_password",
        "s3_access_key",
        "s3_secret_key",
    }


def test_alertmanager_is_configured_for_prometheus_email_routing() -> None:
    config = _compose_config()
    alertmanager = config["services"]["alertmanager"]
    assert alertmanager["image"] == "prom/alertmanager:v0.34.0"
    assert alertmanager["restart"] == "unless-stopped"
    assert alertmanager["user"] == "0:0"
    assert alertmanager["read_only"] is True
    assert alertmanager["cap_drop"] == ["ALL"]
    assert alertmanager["cap_add"] == ["DAC_OVERRIDE"]
    assert "no-new-privileges:true" in alertmanager["security_opt"]
    assert {secret["source"] for secret in alertmanager["secrets"]} == {
        "alertmanager_smtp_password"
    }

    prometheus = (ROOT / "prometheus/prometheus.yml").read_text()
    assert 'targets: ["alertmanager:9093"]' in prometheus

    routing = (ROOT / "alertmanager/alertmanager.yml").read_text()
    assert "smtp_auth_password_file: /run/secrets/smtp_password" in routing
    assert "send_resolved: true" in routing
    assert "1234" not in routing

    alerts = (ROOT / "prometheus/alerts.yml").read_text()
    assert 'webcam_discovery_run_total{result="failure"}' in alerts
    assert "WebcamMaintenanceSequenceSucceeded" in alerts
    assert 'result="success"} > time() - 900' in alerts

    dashboard = json.loads(
        (ROOT / "grafana/dashboards/infrastructure-health.json").read_text()
    )
    scrape_health = next(panel for panel in dashboard["panels"] if panel["id"] == 1)
    assert any("alertmanager" in target["expr"] for target in scrape_health["targets"])


def test_systemd_only_orchestrates_compose_and_scheduled_jobs() -> None:
    directory = ROOT / "deployment/systemd/pilot"
    stack = (directory / "webcam-stack.service").read_text()
    maintenance_timer = (directory / "webcam-maintenance.timer").read_text()
    maintenance = (directory / "run-maintenance-sequence").read_text()

    assert "--profile application --profile monitoring up -d" in stack
    assert "python -m ingestion" not in stack
    assert "OnCalendar=*-*-* 12:00:00 UTC" in maintenance_timer
    expected = [
        "storage.s3_spool_cleanup",
        "run-discovery windy",
        "run-discovery fintraffic",
        "run-discovery skaping",
        "database.database_backup",
    ]
    positions = [maintenance.index(value) for value in expected]
    assert positions == sorted(positions)
    assert "flock -n" in maintenance


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


def test_quiet_maintenance_uses_production_order_and_optional_summary() -> None:
    maintenance = (ROOT / "deployment/benchmarks/run-quiet-maintenance").read_text()
    positions = [
        maintenance.index("storage.s3_spool_cleanup"),
        maintenance.index("discovery.windy.windy_discovery_workflow"),
        maintenance.index("discovery.fintraffic.fintraffic_discovery_workflow"),
        maintenance.index("discovery.skaping.skaping_discovery_workflow"),
        maintenance.index("database.database_backup"),
        maintenance.index("discovery_summary_alert notify"),
    ]
    assert positions == sorted(positions)

    justfile = (ROOT / "justfile").read_text()
    assert "two-hour-full-alert-test:" in justfile
    assert "just ingestion-test-three-networks 2h 1 true" in justfile


def test_checkpoint13_unquiet_crashes_process_without_operator_stopping_service() -> None:
    runner = (
        ROOT / "deployment/benchmarks/run-checkpoint13-unquiet"
    ).read_text()

    assert "/bin/sh -c 'kill -TERM 1'" in runner
    assert "compose --env-file .env kill" not in runner
    assert "RestartCount" in runner
    assert "restarted_and_healthy" in runner
    assert runner.index("windy_termination_started") < runner.index(
        "postgres_restart_started"
    ) < runner.index("maintenance_started")
