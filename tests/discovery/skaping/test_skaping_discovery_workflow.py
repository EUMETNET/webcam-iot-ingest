import pytest

from database.registry_queries import RegistrySnapshot
from discovery.skaping.skaping_discovery_workflow import build_discovery_snapshot
from discovery.skaping.skaping_source_access import SkapingDiscoveryError


def point(point_id, *, media_type="image", label="North") -> dict:
    return {
        "id": point_id,
        "type": media_type,
        "label": label,
        "title": "Public title",
        "url": "https://example.test/view",
        "tags": ["mountain", "snow"],
        "meta-title": "Metadata title",
        "meta-description": "Metadata description",
        "meta-keywords": "mountain,snow",
    }


def camera(
    camera_id,
    *,
    latitude=45.0,
    longitude=6.0,
    altitude=None,
    country="FR",
    points=None,
) -> dict:
    return {
        "id": camera_id,
        "latitude": latitude,
        "longitude": longitude,
        "altitude": altitude,
        "country_code": country,
        "label": "Summit camera",
        "customer": "Example customer",
        "customer_id": 42,
        "time_zone": "Europe/Paris",
        "point_of_views": points if points is not None else [point(11)],
    }


def empty_registry() -> RegistrySnapshot:
    return RegistrySnapshot(sites={}, source_streams={})


def build(*cameras: dict, stored=None):
    return build_discovery_snapshot(
        list(cameras),
        stored or empty_registry(),
        member_countries=("FR", "FI", "CH"),
    )


def test_maps_camera_and_image_point_with_stable_ids_and_full_metadata() -> None:
    result = build(camera(7, altitude="1234.5"))

    assert result.cameras_seen == 1
    assert result.cameras_accepted == 1
    assert result.image_points_of_view_accepted == 1
    assert result.sites[0].site_id == "ska7"
    assert result.sites[0].provider_site_id == "7"
    assert result.sites[0].latitude == 45.0
    assert result.sites[0].longitude == 6.0
    assert result.sites[0].altitude == 1234.5
    assert result.sites[0].country == "FR"
    assert result.sites[0].provider_metadata["customer"] == "Example customer"
    assert result.sites[0].provider_metadata["time_zone"] == "Europe/Paris"
    stream = result.source_streams[0]
    assert stream.source_stream_id == "ska7POV11"
    assert stream.provider_source_stream_id == "11"
    assert stream.selected_rendition == "mini"
    assert stream.provider_metadata["tags"] == ["mountain", "snow"]
    assert stream.provider_metadata["meta-description"] == "Metadata description"


def test_excludes_non_image_points_but_keeps_camera_site() -> None:
    result = build(
        camera(
            7,
            points=[
                point(1),
                point(2, media_type="video"),
                point(3, media_type="frame"),
            ],
        ),
        camera(8, points=[point(4, media_type="video")]),
    )

    assert len(result.sites) == 2
    assert len(result.source_streams) == 1
    assert result.points_of_view_seen == 4
    assert result.non_image_points_of_view_excluded == 3


def test_excludes_known_non_member_country_but_accepts_missing_country() -> None:
    result = build(
        camera(1, country="US"),
        camera(2, country=None),
    )

    assert result.cameras_seen == 2
    assert result.cameras_excluded_country == 1
    assert [site.provider_site_id for site in result.sites] == ["2"]
    assert result.sites[0].country is None


def test_preserves_ids_altitude_and_country_until_coordinates_change() -> None:
    stored = RegistrySnapshot(
        sites={
            "site-kept": {
                "site_id": "site-kept",
                "provider_site_id": "7",
                "latitude": 45.0,
                "longitude": 6.0,
                "altitude": 99.0,
                "country": "CH",
                "provider_metadata": {},
            }
        },
        source_streams={
            "stream-kept": {
                "source_stream_id": "stream-kept",
                "site_id": "site-kept",
                "provider_source_stream_id": "11",
            }
        },
    )
    unchanged = build(camera(7, country=None), stored=stored)
    moved = build(camera(7, longitude=6.1, country=None), stored=stored)

    assert unchanged.sites[0].site_id == "site-kept"
    assert unchanged.sites[0].altitude == 99.0
    assert unchanged.sites[0].country == "CH"
    assert unchanged.source_streams[0].source_stream_id == "stream-kept"
    assert moved.sites[0].altitude is None
    assert moved.sites[0].country is None


def test_identifier_collision_is_resolved_deterministically() -> None:
    result = build(
        camera("a-b", longitude=6.0),
        camera("ab", longitude=6.1),
    )
    assert len({site.site_id for site in result.sites}) == 2
    assert all(site.site_id.isalnum() for site in result.sites)


def test_rejects_duplicate_camera_and_point_ids() -> None:
    with pytest.raises(SkapingDiscoveryError, match="duplicate Skaping camera"):
        build(camera(1), camera(1))
    with pytest.raises(SkapingDiscoveryError, match="duplicate point of view"):
        build(camera(1, points=[point(2), point(2)]))


@pytest.mark.parametrize(
    "bad_camera",
    [
        camera(""),
        camera(1, latitude=91),
        camera(1, longitude="unknown"),
        camera(1, altitude="unknown"),
        camera(1) | {"point_of_views": None},
        camera(1, points=[point(1) | {"type": None}]),
        camera(1, points=[point("")]),
    ],
)
def test_rejects_malformed_camera_or_point(bad_camera) -> None:
    with pytest.raises(SkapingDiscoveryError):
        build(bad_camera)
