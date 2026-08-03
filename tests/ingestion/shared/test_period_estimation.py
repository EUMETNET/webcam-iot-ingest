from dataclasses import replace
from datetime import datetime, timedelta, timezone

from database.registry_queries import (
    DueSourceStream,
    build_ema_update_candidate,
    use_direct_period_replacement,
)
from ingestion.worker import _ema_update_allowed


def job(
    *,
    source_stream_id: str = "stream-1",
    provider_timestamp: datetime | None = None,
    estimate: float | None = None,
) -> DueSourceStream:
    return DueSourceStream(
        source_stream_id=source_stream_id,
        site_id="site-1",
        network_id="win",
        provider_site_id="provider-site",
        provider_source_stream_id="provider-stream",
        selected_rendition="default",
        site_metadata={},
        source_stream_metadata={},
        latitude=50,
        longitude=4,
        altitude=None,
        country="BE",
        corrected_latitude=None,
        corrected_longitude=None,
        corrected_altitude=None,
        last_download_timestamp=None,
        last_observed_provider_timestamp=provider_timestamp,
        last_observed_image_marker=None,
        last_processed_timestamp=None,
        estimated_source_stream_period=estimate,
    )


def test_modulo_decision_is_stable_and_near_one_tenth() -> None:
    timestamp = datetime(2026, 8, 3, 12, tzinfo=timezone.utc)
    selected = sum(
        use_direct_period_replacement(f"stream-{index}", timestamp, 10)
        for index in range(10_000)
    )

    assert 900 <= selected <= 1_100
    assert use_direct_period_replacement("stream-1", timestamp, 10) == (
        use_direct_period_replacement("stream-1", timestamp, 10)
    )


def test_direct_replacement_requires_an_existing_estimate() -> None:
    previous = datetime(2026, 8, 3, 12, tzinfo=timezone.utc)
    current = previous + timedelta(seconds=900)
    selected_id = next(
        f"stream-{index}"
        for index in range(100)
        if use_direct_period_replacement(f"stream-{index}", current, 10)
    )

    initial = build_ema_update_candidate(
        job(
            source_stream_id=selected_id,
            provider_timestamp=previous,
            estimate=None,
        ),
        provider_update_timestamp=current,
        ema_alpha=0.2,
        running_minimum_floor_seconds=300,
        direct_replacement_modulus=10,
    )
    replacement = build_ema_update_candidate(
        job(
            source_stream_id=selected_id,
            provider_timestamp=previous,
            estimate=300,
        ),
        provider_update_timestamp=current,
        ema_alpha=0.2,
        running_minimum_floor_seconds=300,
        direct_replacement_modulus=10,
    )

    assert initial is not None
    assert initial.update_method == "initial"
    assert initial.estimated_source_stream_period == 900
    assert replacement is not None
    assert replacement.update_method == "direct_replacement"
    assert replacement.estimated_source_stream_period == 900


def test_reset_sequence_learns_before_replacement_can_run() -> None:
    old = datetime(2026, 8, 3, 11, tzinfo=timezone.utc)
    first_epoch_timestamp = datetime(2026, 8, 3, 12, tzinfo=timezone.utc)
    second_epoch_timestamp = first_epoch_timestamp + timedelta(seconds=300)

    epoch_one = build_ema_update_candidate(
        job(provider_timestamp=old, estimate=None),
        provider_update_timestamp=first_epoch_timestamp,
        ema_alpha=0.2,
        running_minimum_floor_seconds=300,
        direct_replacement_modulus=1,
    )
    epoch_two = build_ema_update_candidate(
        replace(
            job(provider_timestamp=old, estimate=None),
            last_observed_provider_timestamp=first_epoch_timestamp,
        ),
        provider_update_timestamp=second_epoch_timestamp,
        ema_alpha=0.2,
        running_minimum_floor_seconds=300,
        direct_replacement_modulus=1,
    )

    assert epoch_one is not None and epoch_one.update_method == "initial"
    assert epoch_two is not None and epoch_two.update_method == "initial"
    assert epoch_two.estimated_source_stream_period == 300
    assert not _ema_update_allowed(1, 10, 300)
    assert _ema_update_allowed(2, 10, 300)
    assert not _ema_update_allowed(2, 300, 300)
