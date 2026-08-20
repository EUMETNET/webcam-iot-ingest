"""Prometheus metrics shared by short-lived provider discovery jobs."""

from __future__ import annotations

from collections import Counter as CollectionCounter
from collections.abc import Mapping
import time

from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    push_to_gateway,
    pushadd_to_gateway,
)

from config.deployment_config import DiscoveryMetricsConfig


DISCOVERY_DURATION_BUCKETS = (
    0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 180, 600, 1200
)
PROVIDER_DURATION_BUCKETS = (
    0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30
)


class DiscoveryMetrics:
    """Collect one run, then persist it through a Prometheus Pushgateway."""

    def __init__(
        self,
        source_network: str,
        config: DiscoveryMetricsConfig,
    ) -> None:
        if not source_network:
            raise ValueError("discovery metrics source network cannot be empty")
        self.source_network = source_network
        self.config = config
        self.events = CollectorRegistry()
        self.state = CollectorRegistry()
        self.runs = Counter(
            "webcam_discovery_run_total",
            "Completed discovery runs",
            ["result"],
            registry=self.events,
        )
        self.duration = Histogram(
            "webcam_discovery_duration_seconds",
            "End-to-end discovery run duration",
            buckets=DISCOVERY_DURATION_BUCKETS,
            registry=self.events,
        )
        self.sources_seen = Counter(
            "webcam_discovery_sources_seen_total",
            "Source streams seen during discovery",
            registry=self.events,
        )
        self.sources_added = Counter(
            "webcam_discovery_sources_added_total",
            "New source streams added during discovery",
            registry=self.events,
        )
        self.sources_updated = Counter(
            "webcam_discovery_sources_updated_total",
            "Existing source streams refreshed during discovery",
            registry=self.events,
        )
        self.sources_disabled = Counter(
            "webcam_discovery_sources_disabled_total",
            "Source streams disabled during discovery",
            ["reason"],
            registry=self.events,
        )
        self.identifier_violations = Counter(
            "webcam_discovery_identifier_violation_total",
            "Discovery failures caused by an internal identifier rule violation",
            registry=self.events,
        )
        self.provider_requests = Counter(
            "webcam_provider_request_total",
            "Provider HTTP request attempts",
            ["endpoint_type", "result"],
            registry=self.events,
        )
        self.provider_duration = Histogram(
            "webcam_provider_request_duration_seconds",
            "Provider HTTP request duration",
            ["endpoint_type"],
            buckets=PROVIDER_DURATION_BUCKETS,
            registry=self.events,
        )
        self.provider_throttling = Counter(
            "webcam_provider_throttling_total",
            "Provider throttling responses",
            registry=self.events,
        )
        self.stream_status = Gauge(
            "webcam_source_stream_status_count",
            "Current source stream count by registry status",
            ["status"],
            registry=self.state,
        )
        self.last_success = Gauge(
            "webcam_discovery_last_success_unixtime",
            "Unix timestamp of the last successful discovery run",
            registry=self.state,
        )
        self.last_duration = Gauge(
            "webcam_discovery_last_duration_seconds",
            "Duration of the latest successful discovery run",
            registry=self.state,
        )

    @classmethod
    def from_environment(cls, source_network: str) -> "DiscoveryMetrics":
        return cls(source_network, DiscoveryMetricsConfig.from_environment())

    def observe_provider_request(
        self,
        endpoint_type: str,
        result: str,
        duration_s: float,
    ) -> None:
        endpoint = _bounded_endpoint(endpoint_type)
        self.provider_requests.labels(endpoint, _bounded_result(result)).inc()
        self.provider_duration.labels(endpoint).observe(max(0.0, duration_s))
        if result == "throttled":
            self.provider_throttling.inc()

    def observe_identifier_violation(self) -> None:
        """Record one provider-independent identifier establishment failure."""
        self.identifier_violations.inc()

    def publish_success(
        self,
        *,
        duration_s: float,
        sources_seen: int,
        sources_added: int,
        sources_updated: int,
        sources_disabled: int,
        status_counts: Mapping[str, int],
    ) -> bool:
        self.runs.labels("success").inc()
        self.duration.observe(max(0.0, duration_s))
        self.sources_seen.inc(max(0, sources_seen))
        self.sources_added.inc(max(0, sources_added))
        self.sources_updated.inc(max(0, sources_updated))
        self.sources_disabled.labels("absent_from_snapshot").inc(
            max(0, sources_disabled)
        )
        for status in ("active", "inactive", "blacklisted"):
            self.stream_status.labels(status).set(
                max(0, int(status_counts.get(status, 0)))
            )
        self.last_success.set(time.time())
        self.last_duration.set(max(0.0, duration_s))
        return self._push(include_state=True)

    def publish_failure(self, *, duration_s: float) -> bool:
        self.runs.labels("failure").inc()
        self.duration.observe(max(0.0, duration_s))
        return self._push(include_state=False)

    def _push(self, *, include_state: bool) -> bool:
        if not self.config.enabled:
            return False
        grouping_key = {"source_network": self.source_network}
        try:
            pushadd_to_gateway(
                self.config.gateway_url,
                job="webcam_discovery_events",
                registry=self.events,
                grouping_key=grouping_key,
                timeout=self.config.push_timeout_s,
            )
            if include_state:
                push_to_gateway(
                    self.config.gateway_url,
                    job="webcam_discovery_state",
                    registry=self.state,
                    grouping_key=grouping_key,
                    timeout=self.config.push_timeout_s,
                )
        except OSError:
            return False
        return True


def _bounded_endpoint(value: str) -> str:
    return value if value in {"summary", "list", "detail"} else "other"


def _bounded_result(value: str) -> str:
    return value if value in {"success", "error", "throttled"} else "error"


def registry_status_counts(
    source_streams: Mapping[str, Mapping[str, object]],
) -> dict[str, int]:
    """Return the three bounded registry-status gauges for one network."""
    counts = CollectionCounter(
        str(stream["status"]) for stream in source_streams.values()
    )
    return {
        status: counts.get(status, 0)
        for status in ("active", "inactive", "blacklisted")
    }
