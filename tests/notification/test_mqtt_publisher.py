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

    topic = MqttPublisher(config(), client=client).publish("T0V0", {"x": 1})

    assert topic == "webcam/T0V0"
    client.publish.assert_called_once_with(
        "webcam/T0V0", '{"x":1}', qos=1, retain=False
    )


def test_retries_and_raises_controlled_error() -> None:
    client = Mock()
    client.publish.side_effect = RuntimeError("broker detail")

    with pytest.raises(MqttPublicationError) as caught:
        MqttPublisher(config(), client=client).publish("T0V0", {})

    assert client.publish.call_count == 2
    assert "broker detail" not in str(caught.value)
