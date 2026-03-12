import base64
import io
from random import randint
import requests

from PIL import Image


def make_jpeg_bytes() -> bytes:
    """Create a minimal valid JPEG with random color."""
    buf = io.BytesIO()
    img = Image.new(
        "RGB", (640, 480), color=(randint(0, 255), randint(0, 255), randint(0, 255))
    )
    img.save(buf, format="JPEG")
    return buf.getvalue()


PAYLOAD = {
    "type": "Feature",
    "geometry": {
        "type": "Point",
        "coordinates": {"lat": 59.91, "lon": 10.75},
    },
    "properties": {
        "network": "test_network",
        "image_datetime": "2026-03-11T12:00:00Z",
        "content": {
            "file": base64.b64encode(make_jpeg_bytes()).decode(),
        },
    },
}


def send_jpg_to_endpoint():
    response = requests.post("http://localhost:8009/upload", json=PAYLOAD)
    body = response.json()
    print(f"Status code: {response.status_code}")
    print(body)


if __name__ == "__main__":
    send_jpg_to_endpoint()
