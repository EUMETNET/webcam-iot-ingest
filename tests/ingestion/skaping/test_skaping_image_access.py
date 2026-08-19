from datetime import UTC, datetime

import httpx
import pytest

from ingestion.skaping.skaping_image_access import (
    SkapingImageAccessError,
    SkapingImageClient,
)


POINTER = "https://api.skaping.test/token/media/latest/mini.jpg"
IMAGE = "https://objects.skaping.test/site/2026/07/23/mini/19-05.jpg"


def _client(handler) -> SkapingImageClient:
    return SkapingImageClient(
        request_timeout_s=2,
        image_timeout_s=2,
        max_image_bytes=1000,
        client=httpx.Client(
            transport=httpx.MockTransport(handler),
            follow_redirects=True,
        ),
    )


def test_etag_and_last_modified_are_obtained_without_head_body() -> None:
    requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, str(request.url)))
        if str(request.url) == POINTER:
            return httpx.Response(302, headers={"Location": IMAGE})
        return httpx.Response(
            200,
            headers={
                "ETag": '"image-v1"',
                "Last-Modified": "Thu, 23 Jul 2026 17:06:01 GMT",
                "Content-Type": "image/jpeg",
            },
            content=b"" if request.method == "HEAD" else b"jpeg",
        )

    client = _client(handler)
    reference = client.get_current_image(
        "272", "mini", {"latest_media": {"mini": POINTER}}
    )

    assert reference.marker == '"image-v1"'
    assert reference.image_url == IMAGE
    assert reference.provider_update_timestamp == datetime(
        2026, 7, 23, 17, 6, 1, tzinfo=UTC
    )
    assert requests == [("HEAD", POINTER), ("HEAD", IMAGE)]
    assert client.download(reference.image_url) == b"jpeg"


def test_download_rejects_an_etag_change_after_freshness_check() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == POINTER:
            return httpx.Response(302, headers={"Location": IMAGE})
        return httpx.Response(
            200,
            headers={
                "ETag": '"head"' if request.method == "HEAD" else '"get"',
                "Content-Type": "image/jpeg",
            },
            content=b"" if request.method == "HEAD" else b"jpeg",
        )

    client = _client(handler)
    reference = client.get_current_image(
        "272", "mini", {"latest_media": {"mini": POINTER}}
    )
    with pytest.raises(SkapingImageAccessError, match="download failed"):
        client.download(reference.image_url)


@pytest.mark.parametrize("last_modified", [None, "not-an-http-date"])
def test_last_modified_is_optional_timing_metadata(last_modified) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == POINTER:
            return httpx.Response(302, headers={"Location": IMAGE})
        headers = {
            "ETag": '"image-v1"',
            "Content-Type": "image/jpeg",
        }
        if last_modified is not None:
            headers["Last-Modified"] = last_modified
        return httpx.Response(
            200,
            headers=headers,
            content=b"" if request.method == "HEAD" else b"jpeg",
        )

    client = _client(handler)
    reference = client.get_current_image(
        "272", "mini", {"latest_media": {"mini": POINTER}}
    )

    assert reference.marker == '"image-v1"'
    assert reference.provider_update_timestamp is None
    assert client.download(reference.image_url) == b"jpeg"


@pytest.mark.parametrize(
    ("rendition", "metadata"),
    [
        ("large", {"latest_media": {"mini": POINTER}}),
        ("mini", {}),
        ("mini", {"latest_media": {"mini": "http://unsafe.test/image.jpg"}}),
    ],
)
def test_invalid_rendition_or_pointer_is_rejected(rendition, metadata) -> None:
    with pytest.raises(SkapingImageAccessError):
        _client(lambda _: httpx.Response(500)).get_current_image(
            "272", rendition, metadata
        )
