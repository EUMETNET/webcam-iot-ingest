import io
from datetime import UTC, datetime

from PIL import Image

from config.deployment_config import TransformationConfig
from ingestion.notification.notification_schema_N0V0 import build_notification
from ingestion.shared.source_image_validation import validate_source_image
from ingestion.transformation.transformation_T0V0 import transform
from storage.s3_storage import StoredObject
from tests.ingestion.windy.test_windy_ingestion_workflow import job


def test_builds_complete_versioned_payload() -> None:
    buffer = io.BytesIO()
    Image.new("RGB", (20, 10), "blue").save(buffer, "PNG")
    source = validate_source_image(buffer.getvalue())
    derived = transform(
        source,
        TransformationConfig("T0V0", 288, 90, 50_000, 200_000, 2.0),
    )
    timestamp = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)

    payload = build_notification(
        job=job(),
        source=source,
        derived=derived,
        derived_stream_id="win42T0V0",
        image_id="20260721T120000Z_win42T0V0.jpg",
        stored=StoredObject("webcam", "T0V0/win/image.jpg", "https://x/image.jpg"),
        download_timestamp=timestamp,
        provider_update_timestamp=timestamp,
        publication_timestamp=timestamp,
    )

    assert payload["schema_version"] == "N0V0"
    assert payload["storage"]["object_key"] == "T0V0/win/image.jpg"
    assert payload["source_stream"]["provider_stream_id"] == "42"
    assert payload["derived_stream"]["transformation_version"] == "T0V0"
    assert payload["timestamps"]["download_timestamp"] == "2026-07-21T12:00:00Z"
    assert payload["derived_image"]["format"] == "JPEG"
