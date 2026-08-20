from unittest.mock import Mock

import pytest

from config.deployment_config import MqttConfig
from ingestion.notification.mqtt_publisher import MqttPublicationError, MqttPublisher


def config(retries: int = 1) -> MqttConfig:
    return MqttConfig("localhost", 1883, False, "webcam", 1, retries, 0)


def test_publishes_compact_json_at_qos_one() -> None:
    result = Mock(rc=0)
    result.is_published.return_value = True
    client = Mock()
    client.publish.return_value = result

    events = []
    topic = MqttPublisher(
        config(),
        client=client,
        event_observer=lambda event, values: events.append((event, values)),
    ).publish("T0", {"x": 1})

    assert topic == "webcam/T0"
    client.publish.assert_called_once_with(
        "webcam/T0", '{"x":1}', qos=1, retain=False
    )
    assert events == [
        ("mqtt_operation", {"version": "T0", "result": "success"})
    ]


@pytest.mark.parametrize("retry_count", [0, 1, 3])
def test_exhausted_retries_emit_one_final_failure(retry_count: int) -> None:
    client = Mock()
    client.publish.side_effect = RuntimeError("broker detail")

    events = []
    with pytest.raises(MqttPublicationError) as caught:
        MqttPublisher(
            config(retry_count),
            client=client,
            event_observer=lambda event, values: events.append((event, values)),
        ).publish("T0", {})

    assert client.publish.call_count == retry_count + 1
    assert "broker detail" not in str(caught.value)
    assert events.count(
        ("retry", {"operation": "mqtt_publish", "reason": "request_failure"})
    ) == retry_count
    assert events.count(
        ("mqtt_operation", {"version": "T0", "result": "failure"})
    ) == 1


@pytest.mark.parametrize("retry_count", [0, 1, 3])
def test_success_after_configured_retries_emits_one_final_success(
    retry_count: int,
) -> None:
    successful = Mock(rc=0)
    successful.is_published.return_value = True
    client = Mock()
    client.publish.side_effect = [
        *[RuntimeError("temporary failure") for _ in range(retry_count)],
        successful,
    ]
    events = []

    topic = MqttPublisher(
        config(retry_count),
        client=client,
        event_observer=lambda event, values: events.append((event, values)),
    ).publish("T0", {})

    assert topic == "webcam/T0"
    assert client.publish.call_count == retry_count + 1
    assert events.count(
        ("retry", {"operation": "mqtt_publish", "reason": "request_failure"})
    ) == retry_count
    assert events.count(
        ("mqtt_operation", {"version": "T0", "result": "success"})
    ) == 1
