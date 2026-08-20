"""Pilot T0 JPEG transformation."""

from __future__ import annotations

from dataclasses import dataclass
import io
from statistics import fmean

from PIL import Image, ImageOps

from config.deployment_config import TransformationConfig
from ingestion.shared.source_image_validation import SourceImage


@dataclass(frozen=True)
class DerivedImage:
    content: bytes
    width: int
    height: int
    format: str
    color_mode: str
    color_depth: int
    size_bytes: int
    image_signature: float
    jpeg_quality: int
    panoramic: bool
    transformation_version: str


def transform(source: SourceImage, config: TransformationConfig) -> DerivedImage:
    with Image.open(io.BytesIO(source.content)) as opened:
        opened.seek(0)
        image = ImageOps.exif_transpose(opened).convert("RGB")
        if image.height > config.max_height_px:
            width = max(1, round(image.width * config.max_height_px / image.height))
            image = image.resize((width, config.max_height_px), Image.Resampling.LANCZOS)
        signature = round(fmean(image.convert("L").get_flattened_data()), 6)
        panoramic = image.width / image.height >= config.panoramic_aspect_ratio
        target = (
            config.panoramic_target_size_bytes
            if panoramic
            else config.target_size_bytes
        )
        content, quality, image = _encode_to_target(
            image, config.jpeg_quality_initial, target
        )
    return DerivedImage(
        content=content,
        width=image.width,
        height=image.height,
        format="JPEG",
        color_mode="RGB",
        color_depth=24,
        size_bytes=len(content),
        image_signature=signature,
        jpeg_quality=quality,
        panoramic=panoramic,
        transformation_version=config.version,
    )


def _encode_to_target(
    initial: Image.Image, initial_quality: int, target: int
) -> tuple[bytes, int, Image.Image]:
    image = initial
    while True:
        for quality in range(initial_quality, 4, -5):
            buffer = io.BytesIO()
            image.save(
                buffer,
                format="JPEG",
                quality=quality,
                optimize=True,
                progressive=True,
            )
            content = buffer.getvalue()
            if len(content) <= target:
                return content, quality, image
        if image.width == 1 and image.height == 1:
            return content, quality, image
        image = image.resize(
            (max(1, round(image.width * 0.9)), max(1, round(image.height * 0.9))),
            Image.Resampling.LANCZOS,
        )
