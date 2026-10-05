from shapely.geometry import box

from access_islands.demo import make_demo
from access_islands.prepare import CountryClipper, prepare


def test_complete_preparation_and_cache_can_be_reopened(tmp_path):
    config = make_demo(tmp_path)
    manifest = prepare(config, tmp_path)
    assert manifest["route_count"] == 9
    assert manifest["land_components"] == 5
    assert manifest["synthetic"]
    before = (tmp_path / "prepared" / "routes.gpkg").stat().st_mtime_ns
    assert prepare(config, tmp_path) == manifest
    assert (tmp_path / "prepared" / "routes.gpkg").stat().st_mtime_ns == before


def test_cached_country_clipping_preserves_boundary_and_holes():
    country = box(0, 0, 100, 100).difference(box(40, 40, 60, 60))
    clipper = CountryClipper(country, tile_size=20)
    for polygon in [box(-5, -5, 35, 35), box(35, 35, 70, 70), box(10, 10, 90, 90)]:
        result = clipper.clip(polygon)
        assert result.is_valid
        assert result.equals(polygon.intersection(country))


def test_old_dedication_cannot_reintroduce_excluded_land(tmp_path):
    import geopandas as gpd

    from access_islands.common import CRS, write_layer
    from access_islands.prepare import normalise_land

    primary = tmp_path / "primary.gpkg"
    dedicated = tmp_path / "dedicated.gpkg"
    write_layer(gpd.GeoDataFrame([{"s16": "Yes", "geometry": box(0, 0, 10, 10)}], crs=CRS), primary, "land")
    write_layer(gpd.GeoDataFrame([{"geometry": box(0, 0, 20, 20)}], crs=CRS), dedicated, "land")
    land, accounting, rejected = normalise_land(
        {"land": {"path": str(primary)}, "dedicated": {"path": str(dedicated)}}, box(-100, -100, 100, 100)
    )
    assert land.geometry.union_all().area == 100
    assert any(x["outcome"] == "superseded_by_current_access_layer" for x in accounting)
    assert rejected.empty


def test_coverage_dates_do_not_invalidate_but_missing_authorities_do():
    from access_islands.prepare import preparation_signature

    a = preparation_signature({}, {}, {"retrieved": "yesterday", "missing_authorities": []})
    b = preparation_signature({}, {}, {"retrieved": "today", "missing_authorities": []})
    c = preparation_signature(
        {}, {}, {"missing_authorities": [{"code": "E06000002", "name": "Middlesbrough"}]}
    )
    assert a == b
    assert a != c
