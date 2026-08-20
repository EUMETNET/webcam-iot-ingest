from __future__ import annotations

import json

from observability.discovery_summary_alert import (
    count_differences,
    read_discovery_counts,
    send_summary_alert,
)


def test_pushgateway_counters_are_read_per_network(monkeypatch) -> None:
    exposition = b"""# TYPE webcam_discovery_sources_seen_total counter
webcam_discovery_sources_seen_total{source_network="win"} 25000
webcam_discovery_sources_seen_total{source_network="fin"} 2263
webcam_discovery_sources_seen_total{source_network="ska"} 41
"""

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return exposition

    monkeypatch.setattr(
        "observability.discovery_summary_alert.urlopen",
        lambda *_args, **_kwargs: Response(),
    )
    assert read_discovery_counts("http://pushgateway:9091") == {
        "win": 25000,
        "fin": 2263,
        "ska": 41,
    }


def test_counter_differences_are_per_network_and_non_negative() -> None:
    assert count_differences(
        {"win": 20, "fin": 10, "ska": 5},
        {"win": 120, "fin": 30, "ska": 4},
    ) == {"win": 100, "fin": 20, "ska": 0}


def test_summary_posts_one_bounded_alert(monkeypatch) -> None:
    captured = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    def fake_urlopen(request, *, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setattr("observability.discovery_summary_alert.urlopen", fake_urlopen)
    send_summary_alert("http://alertmanager:9093", {"win": 10, "fin": 2, "ska": 1})

    request = captured["request"]
    payload = json.loads(request.data)
    assert request.full_url == "http://alertmanager:9093/api/v2/alerts"
    assert len(payload) == 1
    assert payload[0]["labels"]["alertname"] == "WebcamMaintenanceSequenceSucceeded"
    assert payload[0]["annotations"]["summary"] == (
        "Maintenance succeeded; discovered 13 webcam streams"
    )
    assert "Windy: 10, Fintraffic: 2, Skaping: 1" in (
        payload[0]["annotations"]["description"]
    )
