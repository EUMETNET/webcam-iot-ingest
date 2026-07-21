from database.registry_queries import RegistrySnapshot
from discovery.windy.windy_discovery_workflow import build_discovery_snapshot


def webcam(
    provider_id: str,
    *,
    latitude: float = 60.0,
    longitude: float = 25.0,
    country: str = "FI",
    categories=None,
    status: str = "active",
) -> dict:
    return {
        "webcamId": provider_id,
        "status": status,
        "title": f"Camera {provider_id}",
        "categories": categories or [],
        "location": {
            "city": "Helsinki",
            "countryCode": country,
            "latitude": latitude,
            "longitude": longitude,
        },
    }


def empty_registry() -> RegistrySnapshot:
    return RegistrySnapshot(sites={}, source_streams={})


def test_normalizes_filters_and_generates_stable_identifiers() -> None:
    result = build_discovery_snapshot(
        [
            webcam("123-a"),
            webcam("indoor", categories=[{"id": "building_indoor"}]),
            webcam("outside-country", country="SE"),
        ],
        empty_registry(),
        allowed_countries={"FI"},
        site_distance_threshold_m=0,
    )

    assert [site.site_id for site in result.sites] == ["win123a"]
    assert [stream.source_stream_id for stream in result.source_streams] == ["win123a"]
    assert result.source_streams[0].selected_rendition == "preview"
    assert result.excluded_count == 2


def test_ignores_provider_status_for_registry_but_preserves_metadata() -> None:
    result = build_discovery_snapshot(
        [webcam("inactive", status="inactive")],
        empty_registry(),
        allowed_countries={"FI"},
        site_distance_threshold_m=0,
    )

    assert result.excluded_count == 0
    assert result.source_streams[0].provider_metadata["status"] == "inactive"


def test_groups_nearby_new_webcams_and_separates_distant_webcams() -> None:
    result = build_discovery_snapshot(
        [
            webcam("1", longitude=25.0),
            webcam("2", longitude=25.0005),
            webcam("3", longitude=26.0),
        ],
        empty_registry(),
        allowed_countries={"FI"},
        site_distance_threshold_m=100,
    )

    streams = {item.provider_source_stream_id: item for item in result.source_streams}
    assert streams["1"].site_id == streams["2"].site_id == "win1"
    assert streams["3"].site_id == "win3"
    assert len(result.sites) == 2


def test_preserves_existing_stream_and_site_identifier_assignment() -> None:
    stored = RegistrySnapshot(
        sites={
            "winoriginal": {
                "site_id": "winoriginal",
                "provider_site_id": "original",
                "latitude": 60.0,
                "longitude": 25.0,
                "altitude": None,
                "country": "FI",
                "provider_metadata": {"old": True},
            }
        },
        source_streams={
            "winkept": {
                "source_stream_id": "winkept",
                "site_id": "winoriginal",
                "provider_source_stream_id": "kept",
            }
        },
    )
    result = build_discovery_snapshot(
        [webcam("kept", longitude=30.0), webcam("new", longitude=25.0001)],
        stored,
        allowed_countries={"FI"},
        site_distance_threshold_m=100,
    )

    streams = {item.provider_source_stream_id: item for item in result.source_streams}
    assert streams["kept"].source_stream_id == "winkept"
    assert streams["kept"].site_id == "winoriginal"
    assert streams["new"].site_id == "winoriginal"


def test_preserves_altitude_until_anchor_coordinates_change() -> None:
    stored = RegistrySnapshot(
        sites={
            "winanchor": {
                "site_id": "winanchor",
                "provider_site_id": "anchor",
                "latitude": 60.0,
                "longitude": 25.0,
                "altitude": 42.0,
                "country": "FI",
                "provider_metadata": {},
            }
        },
        source_streams={
            "winanchor": {
                "source_stream_id": "winanchor",
                "site_id": "winanchor",
                "provider_source_stream_id": "anchor",
            }
        },
    )

    unchanged = build_discovery_snapshot(
        [webcam("anchor")],
        stored,
        allowed_countries={"FI"},
        site_distance_threshold_m=10,
    )
    moved = build_discovery_snapshot(
        [webcam("anchor", latitude=60.001)],
        stored,
        allowed_countries={"FI"},
        site_distance_threshold_m=10,
    )

    assert unchanged.sites[0].altitude == 42.0
    assert moved.sites[0].altitude is None


def test_identifier_collision_gets_deterministic_alphanumeric_suffix() -> None:
    result = build_discovery_snapshot(
        [webcam("a-b"), webcam("ab", longitude=26.0)],
        empty_registry(),
        allowed_countries={"FI"},
        site_distance_threshold_m=0,
    )
    identifiers = [item.source_stream_id for item in result.source_streams]
    assert len(set(identifiers)) == 2
    assert all(identifier.isalnum() for identifier in identifiers)
