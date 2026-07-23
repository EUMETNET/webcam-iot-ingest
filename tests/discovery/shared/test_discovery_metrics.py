from prometheus_client import generate_latest

from config.deployment_config import DiscoveryMetricsConfig
from discovery.shared.discovery_metrics import (
    DiscoveryMetrics,
    registry_status_counts,
)


def config(*, enabled: bool = True) -> DiscoveryMetricsConfig:
    return DiscoveryMetricsConfig(
        enabled=enabled,
        gateway_url="http://pushgateway.test:9091",
        push_timeout_s=2,
    )


def test_success_emits_architecture_metrics_and_replaces_state(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(
        "discovery.shared.discovery_metrics.pushadd_to_gateway",
        lambda *args, **kwargs: calls.append(("add", args, kwargs)),
    )
    monkeypatch.setattr(
        "discovery.shared.discovery_metrics.push_to_gateway",
        lambda *args, **kwargs: calls.append(("replace", args, kwargs)),
    )
    metrics = DiscoveryMetrics("ska", config())
    metrics.observe_provider_request("summary", "success", 0.12)

    published = metrics.publish_success(
        duration_s=0.5,
        sources_seen=41,
        sources_added=2,
        sources_updated=39,
        sources_disabled=1,
        status_counts={"active": 40, "inactive": 1, "blacklisted": 0},
    )

    assert published is True
    assert [call[0] for call in calls] == ["add", "replace"]
    assert all(
        call[2]["grouping_key"] == {"source_network": "ska"}
        for call in calls
    )
    events = generate_latest(metrics.events).decode()
    state = generate_latest(metrics.state).decode()
    assert 'webcam_discovery_run_total{result="success"} 1.0' in events
    assert "webcam_discovery_sources_seen_total 41.0" in events
    assert (
        'webcam_provider_request_total{endpoint_type="summary",result="success"} 1.0'
        in events
    )
    assert 'webcam_source_stream_status_count{status="active"} 40.0' in state
    assert "webcam_discovery_last_duration_seconds 0.5" in state


def test_failure_emits_events_without_replacing_last_success(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(
        "discovery.shared.discovery_metrics.pushadd_to_gateway",
        lambda *args, **kwargs: calls.append("add"),
    )
    monkeypatch.setattr(
        "discovery.shared.discovery_metrics.push_to_gateway",
        lambda *args, **kwargs: calls.append("replace"),
    )
    metrics = DiscoveryMetrics("ska", config())

    assert metrics.publish_failure(duration_s=1.2) is True

    assert calls == ["add"]
    events = generate_latest(metrics.events).decode()
    assert 'webcam_discovery_run_total{result="failure"} 1.0' in events


def test_disabled_or_unreachable_gateway_does_not_fail_discovery(
    monkeypatch,
) -> None:
    disabled = DiscoveryMetrics("ska", config(enabled=False))
    assert (
        disabled.publish_success(
            duration_s=0.1,
            sources_seen=1,
            sources_added=0,
            sources_updated=0,
            sources_disabled=0,
            status_counts={},
        )
        is False
    )

    monkeypatch.setattr(
        "discovery.shared.discovery_metrics.pushadd_to_gateway",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("offline")),
    )
    unreachable = DiscoveryMetrics("ska", config())
    assert unreachable.publish_failure(duration_s=0.1) is False


def test_registry_status_counts_uses_bounded_statuses() -> None:
    assert registry_status_counts(
        {
            "a": {"status": "active"},
            "b": {"status": "active"},
            "c": {"status": "inactive"},
        }
    ) == {"active": 2, "inactive": 1, "blacklisted": 0}
