import io

from PIL import Image
import pytest

from ingestion.shared.source_image_validation import (
    InvalidSourceImageError,
    validate_source_image,
)


def encoded_image(image_format: str = "JPEG") -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (16, 9), (30, 60, 90)).save(buffer, format=image_format)
    return buffer.getvalue()


def test_decodes_complete_image_and_extracts_metadata() -> None:
    content = encoded_image()

    image = validate_source_image(content)

    assert image.content == content
    assert (image.width, image.height) == (16, 9)
    assert image.format == "JPEG"
    assert image.color_mode == "RGB"
    assert image.size_bytes == len(content)


@pytest.mark.parametrize("content", [b"", b"not an image", encoded_image()[:-20]])
def test_rejects_empty_invalid_or_truncated_images(content: bytes) -> None:
    with pytest.raises(InvalidSourceImageError):
        validate_source_image(content)
