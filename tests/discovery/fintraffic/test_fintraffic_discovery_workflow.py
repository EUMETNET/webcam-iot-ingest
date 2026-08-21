import pytest

from database.registry_queries import RegistrySnapshot
from discovery.fintraffic.fintraffic_discovery_workflow import (
    _expand_station_details,
    build_discovery_snapshot,
)
from discovery.fintraffic.fintraffic_source_access import FintrafficDiscoveryError


def station(
    station_id: str,
    *,
    status: str = "GATHERING",
    coordinates=(24.0, 60.0, 0.0),
    presets=None,
) -> dict:
    return {
        "type": "Feature",
        "id": station_id,
        "geometry": {"type": "Point", "coordinates": list(coordinates)},
        "properties": {
            "id": station_id,
            "name": f"Station {station_id}",
            "collectionStatus": status,
            "presets": presets
            if presets is not None
            else [{"id": f"{station_id}01", "inCollection": True}],
        },
    }


def snapshot(*features: dict) -> dict:
    return {"type": "FeatureCollection", "features": list(features)}


def empty_registry() -> RegistrySnapshot:
    return RegistrySnapshot(sites={}, source_streams={})


def test_normalizes_station_and_preset_with_stable_identifiers() -> None:
    result = build_discovery_snapshot(snapshot(station("C01503")), empty_registry())

    assert result.provider_stations_seen == 1
    assert result.stations_accepted == 1
    assert result.presets_accepted == 1
    assert result.sites[0].site_id == "finC01503"
    assert result.sites[0].provider_site_id == "C01503"
    assert result.sites[0].longitude == 24.0
    assert result.sites[0].latitude == 60.0
    assert result.sites[0].altitude is None
    assert result.sites[0].country == "FI"
    assert result.sites[0].provider_metadata["geometry"]["coordinates"][2] == 0.0
    assert result.source_streams[0].source_stream_id == "finC0150301"
    assert result.source_streams[0].selected_rendition == "full_jpeg"


def test_filters_non_gathering_stations_and_non_collection_presets() -> None:
    result = build_discovery_snapshot(
        snapshot(
            station("OFF", status="REMOVED"),
            station(
                "ON",
                presets=[
                    {"id": "ON01", "inCollection": True},
                    {"id": "ON02", "inCollection": False},
                ],
            ),
        ),
        empty_registry(),
    )

    assert result.provider_stations_seen == 2
    assert result.stations_excluded == 1
    assert result.presets_seen == 3
    assert result.presets_accepted == 1
    assert result.presets_excluded == 2


def test_expands_only_eligible_stations_with_detailed_metadata() -> None:
    compact_gathering = station("ON")
    compact_removed = station("OFF", status="REMOVED_TEMPORARILY")
    detail = station(
        "ON",
        presets=[
            {
                "id": "ON01",
                "inCollection": True,
                "presentationName": "Road surface",
                "direction": "SPECIAL_DIRECTION",
            }
        ],
    )
    detail["properties"]["purpose"] = "keli"

    class Client:
        def fetch_station(self, station_id):
            assert station_id == "ON"
            return detail

    expanded = _expand_station_details(
        snapshot(compact_gathering, compact_removed),
        Client(),
        selected_collection_status="GATHERING",
    )
    result = build_discovery_snapshot(expanded, empty_registry())

    assert result.sites[0].provider_metadata["properties"]["purpose"] == "keli"
    assert (
        result.source_streams[0].provider_metadata["presentationName"]
        == "Road surface"
    )
    assert result.source_streams[0].provider_metadata["direction"] == "SPECIAL_DIRECTION"


def test_preserves_existing_identifiers_and_altitude_until_coordinates_change() -> None:
    stored = RegistrySnapshot(
        sites={
            "finkept": {
                "site_id": "finkept",
                "provider_site_id": "C1",
                "latitude": 60.0,
                "longitude": 24.0,
                "altitude": 42.0,
                "country": "FI",
                "provider_metadata": {},
            }
        },
        source_streams={
            "streamkept": {
                "source_stream_id": "streamkept",
                "site_id": "finkept",
                "provider_source_stream_id": "C101",
            }
        },
    )

    unchanged = build_discovery_snapshot(snapshot(station("C1")), stored)
    moved = build_discovery_snapshot(
        snapshot(station("C1", coordinates=(24.1, 60.0, 0.0))), stored
    )

    assert unchanged.sites[0].site_id == "finkept"
    assert unchanged.sites[0].altitude == 42.0
    assert unchanged.source_streams[0].source_stream_id == "streamkept"
    assert moved.sites[0].altitude is None


def test_identifier_collision_is_resolved_deterministically() -> None:
    result = build_discovery_snapshot(
        snapshot(
            station("a-b", coordinates=(24.0, 60.0)),
            station("ab", coordinates=(25.0, 61.0)),
        ),
        empty_registry(),
    )
    assert len({site.site_id for site in result.sites}) == 2
    assert all(site.site_id.isalnum() for site in result.sites)
    assert all(stream.source_stream_id.isalnum() for stream in result.source_streams)


def test_rejects_preset_that_does_not_embed_station_identifier() -> None:
    with pytest.raises(FintrafficDiscoveryError, match="does not start with station"):
        build_discovery_snapshot(
            snapshot(
                station(
                    "C01503",
                    presets=[{"id": "C9999901", "inCollection": True}],
                )
            ),
            empty_registry(),
        )


def test_rejects_globally_duplicate_preset_identifier() -> None:
    with pytest.raises(FintrafficDiscoveryError, match="duplicate preset"):
        build_discovery_snapshot(
            snapshot(
                station("C1", presets=[{"id": "C101", "inCollection": True}]),
                station("C101", presets=[{"id": "C101", "inCollection": True}]),
            ),
            empty_registry(),
        )


@pytest.mark.parametrize(
    "bad_station",
    [
        station("C1") | {"id": ""},
        station("C1") | {"geometry": {"type": "LineString", "coordinates": []}},
        station("C1") | {"geometry": {"type": "Point", "coordinates": [200, 60]}},
        station("C1") | {"properties": []},
        station("C1", presets=[{"id": "C101", "inCollection": "true"}]),
    ],
)
def test_rejects_malformed_station_or_preset(bad_station) -> None:
    with pytest.raises(FintrafficDiscoveryError):
        build_discovery_snapshot(snapshot(bad_station), empty_registry())


def test_rejects_duplicate_station_and_preset_identifiers() -> None:
    with pytest.raises(FintrafficDiscoveryError, match="duplicate Fintraffic station"):
        build_discovery_snapshot(snapshot(station("C1"), station("C1")), empty_registry())
    with pytest.raises(FintrafficDiscoveryError, match="duplicate preset"):
        build_discovery_snapshot(
            snapshot(
                station(
                    "C1",
                    presets=[
                        {"id": "C101", "inCollection": True},
                        {"id": "C101", "inCollection": True},
                    ],
                )
            ),
            empty_registry(),
        )
