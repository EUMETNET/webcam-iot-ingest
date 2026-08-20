"""Publish one harmless N0V0-labelled message to the configured local broker."""

import json

from config.deployment_config import MqttConfig
from ingestion.notification.mqtt_publisher import MqttPublisher


def main() -> None:
    with MqttPublisher(MqttConfig.from_environment()) as publisher:
        topic = publisher.publish(
            "T0", {"schema_version": "N0V0", "smoke_test": True}
        )
    print(json.dumps({"published": True, "topic": topic}, sort_keys=True))


if __name__ == "__main__":
    main()
