from __future__ import annotations

import json

from observability.discovery_summary_alert import (
    read_discovery_result,
    send_summary_alert,
)


def test_reads_final_structured_discovery_result(tmp_path) -> None:
    result = tmp_path / "windy.jsonl"
    result.write_text(
        '{"windy_country_classification": {"small": ["DK"]}}\n'
        'an incidental non-JSON line\n'
        '{"source_streams": 24916, "streams_inserted": 48}\n'
    )

    assert read_discovery_result(result) == 24916


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
