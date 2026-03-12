import base64
import io
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from PIL import Image


def make_jpeg_bytes(x, y) -> bytes:
    """Create a minimal valid JPEG in memory."""
    buf = io.BytesIO()
    img = Image.new("RGB", (x, y), color=(255, 0, 0))
    img.save(buf, format="JPEG")
    return buf.getvalue()


def make_png_bytes(x, y) -> bytes:
    """Create a minimal valid PNG in memory."""
    buf = io.BytesIO()
    img = Image.new("RGB", (x, y), color=(255, 0, 0))
    img.save(buf, format="PNG")
    return buf.getvalue()


PAYLOAD = {
    "type": "Feature",
    "geometry": {
        "type": "Point",
        "coordinates": {"lat": 59.91, "lon": 10.75},
    },
    "properties": {
        "image_datetime": "2026-03-11T12:00:00Z",
        "network": "test_network",
        "content": {
            "file": base64.b64encode(make_jpeg_bytes(640, 480)).decode(),
        },
    },
}

PAYLOAD_LARGE = {
    "type": "Feature",
    "geometry": {
        "type": "Point",
        "coordinates": {"lat": 52.91, "lon": 10.75},
    },
    "properties": {
        "image_datetime": "2026-03-11T14:00:00Z",
        "network": "test_network",
        "content": {
            "file": base64.b64encode(make_jpeg_bytes(1280, 960)).decode(),
        },
    },
}

PAYLOAD_PNG = {
    "type": "Feature",
    "geometry": {
        "type": "Point",
        "coordinates": {"lat": 52.91, "lon": 10.75},
    },
    "properties": {
        "image_datetime": "2026-03-11T14:00:00Z",
        "network": "test_network",
        "content": {
            "file": base64.b64encode(make_png_bytes(640, 480)).decode(),
        },
    },
}


@pytest.fixture()
def client():
    with (
        patch("api.main.connect_mqtt", return_value=MagicMock()),
        patch("api.main.IngestToPipeline", return_value=MagicMock()),
    ):
        from api.main import app

        return TestClient(app)


def test_upload_jpeg_success(client):
    with (
        patch(
            "api.main.upload_fileobject",
            new_callable=AsyncMock,
            return_value="https://example.bucket.com/webcam/image.jpg",
        ),
        patch("api.main.send_message") as mock_mqtt,
    ):
        response = client.post("/upload", json=PAYLOAD)

    assert response.status_code == 200, f"Unexpected 422: {response.json()}"
    body = response.json()
    assert body["object_url"].endswith(".jpg")
    assert "uploaded" in body
    assert "object_url" in body


def test_upload_corrupted_image_rejected(client):
    bad_payload = {
        **PAYLOAD,
        "properties": {
            **PAYLOAD["properties"],
            "content": {"file": base64.b64encode(b"not-an-image").decode()},
        },
    }
    response = client.post("/upload", json=bad_payload)
    assert response.status_code == 422


def test_upload_missing_content_rejected(client):
    bad_payload = {
        **PAYLOAD,
        "properties": {
            "image_datetime": "2026-03-11T12:00:00Z",
            # content is required but omitted
        },
    }
    response = client.post("/upload", json=bad_payload)
    assert response.status_code == 422


def test_upload_missing_datetime_rejected(client):
    bad_payload = {
        **PAYLOAD,
        "properties": {
            # image_datetime is required but omitted
            "content": PAYLOAD["properties"]["content"],
        },
    }
    response = client.post("/upload", json=bad_payload)
    assert response.status_code == 422


def test_upload_invalid_datetime_format_rejected(client):
    bad_payload = {
        **PAYLOAD,
        "properties": {
            **PAYLOAD["properties"],
            "image_datetime": "11-03-2026 12:00:00",  # not ISO 8601
        },
    }
    response = client.post("/upload", json=bad_payload)
    assert response.status_code == 422


def test_upload_non_utc_datetime_rejected(client):
    bad_payload = {
        **PAYLOAD,
        "properties": {
            **PAYLOAD["properties"],
            "image_datetime": "2026-03-11T12:00:00+02:00",  # valid ISO 8601 but not UTC
        },
    }
    response = client.post("/upload", json=bad_payload)
    assert response.status_code == 422


def test_upload_invalid_direction_rejected(client):
    bad_payload = {
        **PAYLOAD,
        "properties": {
            **PAYLOAD["properties"],
            "direction": 400,  # int out of range (ge=0, le=359)
        },
    }
    response = client.post("/upload", json=bad_payload)
    assert response.status_code == 422


def test_upload_missing_geometry_rejected(client):
    bad_payload = {k: v for k, v in PAYLOAD.items() if k != "geometry"}
    response = client.post("/upload", json=bad_payload)
    assert response.status_code == 422


def test_upload_missing_network_rejected(client):
    bad_payload = {
        **PAYLOAD,
        "properties": {**PAYLOAD["properties"], "network": None},
    }
    response = client.post("/upload", json=bad_payload)
    assert response.status_code == 422


def test_upload_png_converted_to_jpeg(client):
    captured = {}

    async def capture_upload(file_name, file_obj, **kwargs):
        captured["bytes"] = file_obj.read()
        return "https://example.bucket.com/webcam/image.jpg"

    with (
        patch("api.main.upload_fileobject", side_effect=capture_upload),
        patch("api.main.send_message"),
    ):
        response = client.post("/upload", json=PAYLOAD_PNG)

    assert response.status_code == 200, f"Unexpected error: {response.json()}"
    img = Image.open(io.BytesIO(captured["bytes"]))
    assert img.format == "JPEG", f"Expected JPEG, got {img.format}"


def test_upload_large_image_resized(client):
    captured = {}

    async def capture_upload(file_name, file_obj, **kwargs):
        captured["bytes"] = file_obj.read()
        return "https://example.bucket.com/webcam/image.jpg"

    with (
        patch("api.main.upload_fileobject", side_effect=capture_upload),
        patch("api.main.send_message"),
    ):
        response = client.post("/upload", json=PAYLOAD_LARGE)

    assert response.status_code == 200, f"Unexpected error: {response.json()}"
    assert "bytes" in captured
    img = Image.open(io.BytesIO(captured["bytes"]))
    assert img.width <= 640 and img.height <= 480, (
        f"Expected image to be resized to at most 640x480, got {img.width}x{img.height}"
    )
