import io
from datetime import UTC, datetime
from unittest.mock import Mock

from PIL import Image

from config.deployment_config import TransformationConfig
from ingestion.shared.ingestion_core import process_source_image
from ingestion.shared.source_image_validation import validate_source_image
from storage.s3_storage import StoredObject
from tests.ingestion.windy.test_windy_ingestion_workflow import job


def test_upload_precedes_notification_and_payload_references_object() -> None:
    events = []
    buffer = io.BytesIO()
    Image.new("RGB", (20, 10), "green").save(buffer, "JPEG")
    source = validate_source_image(buffer.getvalue())
    storage = Mock(prefix="pilot")

    def upload(key, content):
        events.append("upload")
        return StoredObject("webcam", key, f"https://objects/{key}")

    storage.upload.side_effect = upload
    publisher = Mock()

    def publish(version, payload):
        events.append("publish")
        assert payload["storage"]["object_key"].startswith("pilot/T0V0/win/")
        return "webcam/T0V0"

    publisher.publish.side_effect = publish

    result = process_source_image(
        job=job(),
        source=source,
        download_timestamp=datetime(2026, 7, 21, 12, 0, tzinfo=UTC),
        provider_update_timestamp=None,
        transformation=TransformationConfig("T0V0", 288, 90, 50_000, 200_000, 2.0),
        storage=storage,
        publisher=publisher,
    )

    assert events == ["upload", "publish"]
    assert result.mqtt_topic == "webcam/T0V0"
