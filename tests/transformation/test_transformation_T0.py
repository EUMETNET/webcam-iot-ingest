import io

from PIL import Image

from config.deployment_config import TransformationConfig
from ingestion.shared.source_image_validation import validate_source_image
from ingestion.transformation.transformation_T0 import transform


def config(**changes) -> TransformationConfig:
    values = dict(
        version="T0",
        max_height_px=288,
        jpeg_quality_initial=90,
        target_size_bytes=50_000,
        panoramic_target_size_bytes=200_000,
        panoramic_aspect_ratio=2.0,
    )
    values.update(changes)
    return TransformationConfig(**values)


def source(width: int, height: int, image_format: str = "PNG"):
    buffer = io.BytesIO()
    image = Image.effect_noise((width, height), 80).convert("RGB")
    image.save(buffer, format=image_format)
    return validate_source_image(buffer.getvalue())


def test_caps_height_proportionally_and_normalizes_jpeg() -> None:
    result = transform(source(1000, 500), config())

    assert (result.width, result.height) == (576, 288)
    assert result.format == "JPEG"
    assert result.color_mode == "RGB"
    assert result.color_depth == 24
    assert result.size_bytes <= 200_000
    assert result.panoramic is True


def test_standard_image_meets_target_and_signature_is_scalar() -> None:
    result = transform(source(400, 400), config(target_size_bytes=8_000))

    assert result.height <= 288
    assert result.size_bytes <= 8_000
    assert isinstance(result.image_signature, float)
    with Image.open(io.BytesIO(result.content)) as decoded:
        decoded.load()
        assert decoded.format == "JPEG"


def test_deterministic_input_has_deterministic_signature() -> None:
    original = source(40, 20)
    assert transform(original, config()).image_signature == transform(
        original, config()
    ).image_signature
