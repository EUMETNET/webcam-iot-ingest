import os
import logging
import json
import time
from paho.mqtt import client as mqtt_client
from fastapi import HTTPException

logger = logging.getLogger(__name__)


mqtt_protocols = {
    "3.1": mqtt_client.MQTTv31,
    "3.1.1": mqtt_client.MQTTv311,
    "5": mqtt_client.MQTTv5,
}

mqtt_topic_prepend = os.getenv("MQTT_TOPIC_PREPEND", "")
if not mqtt_topic_prepend or mqtt_topic_prepend == "/":
    mqtt_topic_prepend = ""
    logger.error(
        "MQTT_TOPIC_PREPEND cannot be just a '/'. Setting topic prepend to empty string."
    )
else:
    mqtt_topic_prepend = (
        mqtt_topic_prepend
        if mqtt_topic_prepend.endswith("/")
        else mqtt_topic_prepend + "/"
    )


def connect_mqtt(mqtt_conf: dict):
    def on_connect(client, userdata, flags, rc, properties=None):
        if rc == 0:
            logger.info("Connected to MQTT Broker!")
        else:
            logger.error(f"Failed to connect, return code  {rc}")

    def on_disconnect(client, userdata, flags, rc, properties):
        logger.warning(f"Disconnected from MQTT broker with result code {str(rc)}")
        if rc != 0:
            _reconnect(client)

    client = mqtt_client.Client(
        mqtt_client.CallbackAPIVersion.VERSION2,
        protocol=mqtt_protocols[os.getenv("MQTT_PROTOCOL_VERSION", "5")],
    )
    client.enable_logger(logger)
    client.username_pw_set(mqtt_conf["username"], mqtt_conf["password"])

    if mqtt_conf["enable_tls"]:
        client.tls_set()

    client.on_connect = on_connect
    client.on_disconnect = on_disconnect

    client.connect(mqtt_conf["host"], mqtt_conf["port"])
    client.loop_start()
    return client


def _reconnect(client: mqtt_client.Client):
    """Attempt to reconnect with backoff (max 5min)."""
    delay = 1
    max_delay = 300
    while True:
        try:
            logger.info(f"Attempting reconnect to MQTT broker in {delay}s...")
            time.sleep(delay)
            client.reconnect()
            logger.info("Reconnected to MQTT broker.")
            return
        except Exception as e:
            logger.error(f"Reconnect failed: {e}")
            delay = min(delay * 2, max_delay)


def send_message(topic: str, message: str, client: object):
    if not topic:
        raise ValueError("MQTT topic must not be empty")
    mqtt_topic = mqtt_topic_prepend + topic
    try:
        if isinstance(message, dict):
            result = client.publish(mqtt_topic, json.dumps(message))
        elif isinstance(message, (str, bytes)):
            result = client.publish(mqtt_topic, message)
        else:
            raise TypeError("Mqtt message of unknown type")

        result.wait_for_publish(timeout=5)
        if result.rc != mqtt_client.MQTT_ERR_SUCCESS:
            raise RuntimeError(f"Publish failed with rc={result.rc}")

    except Exception as e:
        logger.critical(str(e))
        raise HTTPException(status_code=500, detail="Failed to publish to mqtt")
