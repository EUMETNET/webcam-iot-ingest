from datetime import UTC, datetime

from ingestion.shared.derived_identifiers import (
    build_derived_stream_id,
    build_image_id,
    build_object_key,
)


def test_builds_architecture_identifiers_and_object_key() -> None:
    timestamp = datetime(2026, 6, 12, 14, 35, 12, tzinfo=UTC)
    stream = build_derived_stream_id("win123456", "T0V0")
    image = build_image_id(stream, timestamp)

    assert stream == "win123456T0V0"
    assert image == "20260612T143512Z_win123456T0V0.jpg"
    assert build_object_key("T0V0", "win", image, timestamp) == (
        "T0V0/win/2026/06/12/14/20260612T143512Z_win123456T0V0.jpg"
    )
