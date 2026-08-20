"""Persistent Prometheus metrics for short-lived maintenance jobs."""

from __future__ import annotations

from collections.abc import Mapping
import time

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram
from prometheus_client import push_to_gateway, pushadd_to_gateway

from config.deployment_config import MaintenanceMetricsConfig


class MaintenanceJobMetrics:
    def __init__(self, job_name: str, config: MaintenanceMetricsConfig) -> None:
        if job_name not in {
            "spool_cleanup",
            "database_backup",
            "database_backup_cleanup",
            "database_restore",
        }:
            raise ValueError("unsupported maintenance job")
        self.job_name = job_name
        self.config = config
        self.events = CollectorRegistry()
        self.state = CollectorRegistry()
        self.runs = Counter(
            "webcam_maintenance_run_total",
            "Completed maintenance runs",
            ["result"],
            registry=self.events,
        )
        self.duration = Histogram(
            "webcam_maintenance_duration_seconds",
            "End-to-end maintenance-job duration",
            buckets=(0.1, 0.5, 1, 2.5, 5, 10, 30, 60, 180, 600),
            registry=self.events,
        )
        self.items = Counter(
            "webcam_maintenance_items_total",
            "Objects processed by a maintenance job",
            ["outcome"],
            registry=self.events,
        )
        self.bytes = Counter(
            "webcam_maintenance_bytes_total",
            "Bytes processed by a maintenance job",
            ["outcome"],
            registry=self.events,
        )
        self.stage_duration = Histogram(
            "webcam_maintenance_stage_duration_seconds",
            "Maintenance-job stage duration",
            ["stage"],
            buckets=(0.01, 0.05, 0.1, 0.5, 1, 2.5, 5, 10, 30, 60, 180),
            registry=self.events,
        )
        self.last_success = Gauge(
            "webcam_maintenance_last_success_unixtime",
            "Unix timestamp of the last successful maintenance job",
            registry=self.state,
        )
        self.last_duration = Gauge(
            "webcam_maintenance_last_duration_seconds",
            "Duration of the latest successful maintenance job",
            registry=self.state,
        )
        self.retention_hours = (
            Gauge(
                "webcam_spool_cleanup_retention_hours",
                "Configured image retention threshold in hours",
                registry=self.state,
            )
            if job_name == "spool_cleanup"
            else None
        )
        self.last_size = (
            Gauge(
                "webcam_database_backup_last_size_bytes",
                "Size of the latest successful database dump",
                registry=self.state,
            )
            if job_name == "database_backup"
            else None
        )

    @classmethod
    def from_environment(cls, job_name: str) -> "MaintenanceJobMetrics":
        return cls(job_name, MaintenanceMetricsConfig.from_environment())

    def publish(
        self,
        *,
        success: bool,
        dry_run: bool = False,
        duration_s: float,
        items: Mapping[str, int] = {},
        bytes_by_outcome: Mapping[str, int] = {},
        stages: Mapping[str, float] = {},
        retention_hours: float | None = None,
        backup_size_bytes: int | None = None,
    ) -> bool:
        result = "dry_run" if dry_run else ("success" if success else "failure")
        self.runs.labels(result).inc()
        self.duration.observe(max(0.0, duration_s))
        for outcome, value in items.items():
            self.items.labels(outcome).inc(max(0, value))
        for outcome, value in bytes_by_outcome.items():
            self.bytes.labels(outcome).inc(max(0, value))
        for stage, value in stages.items():
            self.stage_duration.labels(stage).observe(max(0.0, value))
        if success and not dry_run:
            self.last_success.set(time.time())
            self.last_duration.set(max(0.0, duration_s))
            if retention_hours is not None and self.retention_hours is not None:
                self.retention_hours.set(retention_hours)
            if backup_size_bytes is not None and self.last_size is not None:
                self.last_size.set(max(0, backup_size_bytes))
        return self._push(include_state=success and not dry_run)

    def _push(self, *, include_state: bool) -> bool:
        if not self.config.enabled:
            return False
        grouping = {"maintenance_job": self.job_name}
        try:
            pushadd_to_gateway(
                self.config.gateway_url,
                job="webcam_maintenance_events",
                registry=self.events,
                grouping_key=grouping,
                timeout=self.config.push_timeout_s,
            )
            if include_state:
                push_to_gateway(
                    self.config.gateway_url,
                    job="webcam_maintenance_state",
                    registry=self.state,
                    grouping_key=grouping,
                    timeout=self.config.push_timeout_s,
                )
        except OSError:
            return False
        return True
