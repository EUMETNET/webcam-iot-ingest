"""Bounded QoS MQTT publication."""

from __future__ import annotations

import json
import time
from typing import Any, Callable

from paho.mqtt import client as mqtt

from config.deployment_config import MqttConfig


class MqttPublicationError(RuntimeError):
    """Notification publication failed after bounded attempts."""


class MqttPublisher:
    def __init__(
        self,
        config: MqttConfig,
        *,
        client: Any | None = None,
        sleep: Callable[[float], None] = time.sleep,
        observer: Callable[[str, str, float], None] | None = None,
    ) -> None:
        self._config = config
        self._sleep = sleep
        self._observer = observer
        self._client = client or mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            protocol=mqtt.MQTTv5,
        )
        if config.tls_enabled:
            self._client.tls_set()
        if client is None:
            self._client.connect(config.host, config.port, keepalive=30)
            self._client.loop_start()

    def close(self) -> None:
        self._client.loop_stop()
        self._client.disconnect()

    def __enter__(self) -> "MqttPublisher":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def publish(self, version: str, payload: dict[str, Any]) -> str:
        started = time.monotonic()
        try:
            result = self._publish(version, payload)
        except Exception:
            if self._observer is not None:
                self._observer("mqtt_publish", "failure", time.monotonic() - started)
            raise
        if self._observer is not None:
            self._observer("mqtt_publish", "success", time.monotonic() - started)
        return result

    def _publish(self, version: str, payload: dict[str, Any]) -> str:
        topic = f"{self._config.topic_prefix}/{version}"
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        last_error: Exception | None = None
        for attempt in range(self._config.retry_count + 1):
            try:
                result = self._client.publish(
                    topic, body, qos=self._config.qos, retain=False
                )
                result.wait_for_publish(timeout=10)
                if result.rc != mqtt.MQTT_ERR_SUCCESS or not result.is_published():
                    raise RuntimeError("broker did not acknowledge publication")
                return topic
            except Exception as error:
                last_error = error
                if attempt < self._config.retry_count and self._config.retry_backoff_s:
                    self._sleep(self._config.retry_backoff_s * (2**attempt))
        raise MqttPublicationError("MQTT notification publication failed") from last_error
