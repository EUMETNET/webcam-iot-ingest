"""Full in-memory source-image decoding and metadata extraction."""

from __future__ import annotations

from dataclasses import dataclass
import io

from PIL import Image, UnidentifiedImageError


class InvalidSourceImageError(ValueError):
    """Downloaded bytes are not a fully decodable supported image."""


@dataclass(frozen=True)
class SourceImage:
    content: bytes
    size_bytes: int
    width: int
    height: int
    format: str
    color_mode: str


def validate_source_image(content: bytes) -> SourceImage:
    if not content:
        raise InvalidSourceImageError("source image is empty")
    try:
        with Image.open(io.BytesIO(content)) as image:
            image.load()
            image_format = image.format
            if not image_format:
                raise InvalidSourceImageError("source image format is unknown")
            return SourceImage(
                content=content,
                size_bytes=len(content),
                width=image.width,
                height=image.height,
                format=image_format,
                color_mode=image.mode,
            )
    except (UnidentifiedImageError, OSError, SyntaxError) as error:
        raise InvalidSourceImageError("source image cannot be fully decoded") from error
