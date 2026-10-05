import geopandas as gpd
import pytest
from shapely.geometry import LineString, MultiPolygon, Point, box

from access_islands.analyse import add_nearest_distance, analyse, form_sites
from access_islands.common import CRS, atomic_json, empty_frame, write_layer


def fixture_inputs(tmp_path, polygons, routes, missing=False):
    root = tmp_path / "data"
    prepared = root / "prepared"
    prepared.mkdir(parents=True)
    land = gpd.GeoDataFrame(
        [
            {"component_id": f"land:{i}", "source_id": f"land:{i}", "name": "", "geometry": p}
            for i, p in enumerate(polygons)
        ],
        crs=CRS,
    )
    write_layer(land, prepared / "land.gpkg", "land")
    bounds = gpd.GeoDataFrame(
        [{"code": "E00000001", "name": "Test area", "geometry": box(-1000, -1000, 2000, 2000)}], crs=CRS
    )
    for role in ["counties", "councils"]:
        write_layer(bounds, prepared / "land.gpkg", role)
    write_layer(bounds if missing else empty_frame(["code", "name"]), prepared / "land.gpkg", "coverage_gaps")
    write_layer(empty_frame(["source_id", "reason"]), prepared / "land.gpkg", "rejected")
    rows = []
    for i, spec in enumerate(routes):
        geom, tier, seed, *nodes = spec
        rows.append(
            {
                "route_index": i,
                "source_id": f"route:{i}",
                "tier": tier,
                "seed": seed,
                "start_node": nodes[0] if nodes else "",
                "end_node": nodes[1] if nodes else "",
                "grade": nodes[2] if len(nodes) > 2 else "0:no:no",
                "reason": "",
                "name": "",
                "geometry": geom,
            }
        )
    write_layer(gpd.GeoDataFrame(rows, crs=CRS), prepared / "routes.gpkg", "routes")
    write_layer(empty_frame(["node_id", "blocking", "tags"]), prepared / "barriers.gpkg", "barriers")
    atomic_json(
        prepared / "accounting.json",
        [{"source_id": f"land:{i}", "component": 0, "outcome": "processed"} for i in range(len(polygons))],
    )
    atomic_json(
        prepared / "manifest.json",
        {"signature": "test", "route_count": len(rows), "unmapped_missing_authorities": [], "sources": {}},
    )
    return root


def statuses(tmp_path, polygons, routes, **kwargs):
    root = fixture_inputs(tmp_path, polygons, routes, kwargs.pop("missing", False))
    output = tmp_path / "out"
    analyse({"analysis": {"tile_size_m": kwargs.pop("tile_size", 20000)}}, root, output)
    return gpd.read_file(output / "results.gpkg", layer="sites")


def test_adjoining_land_and_disconnected_internal_path(tmp_path):
    sites = statuses(
        tmp_path,
        [box(0, 0, 10, 10), box(10, 0, 20, 10), box(100, 0, 110, 10)],
        [
            (LineString([(-20, -10), (50, -10)]), "inferred", True, "osm:1", "osm:2"),
            (LineString([(5, -10), (5, 5)]), "official", False),
            (LineString([(101, 5), (109, 5)]), "official", False),
        ],
    )
    assert len(sites) == 2
    assert set(sites.status) == {"access_evidenced", "likely_access_island"}
    assert sites.loc[sites.status == "access_evidenced", "parcel_count"].iloc[0] == 2


def test_tiny_geometry_is_retained_for_review_and_threshold_is_configurable(tmp_path):
    root = fixture_inputs(
        tmp_path,
        [box(0, 0, 1, 1), box(100, 0, 111, 9), box(200, 0, 210, 10)],
        [(LineString([(-20, -100), (400, -100)]), "inferred", True, "osm:1", "osm:2")],
    )
    output = tmp_path / "out"
    analyse({}, root, output)
    sites = gpd.read_file(output / "results.gpkg", layer="sites").sort_values("area_ha")
    assert len(sites) == 3
    assert sites.status.tolist() == ["uncertain", "uncertain", "likely_access_island"]
    assert sites.reasons.iloc[:2].str.contains("very_small_site_geometry").all()
    ids = set(sites.site_id)
    analyse({"analysis": {"min_candidate_area_m2": 0}}, root, output)
    sites = gpd.read_file(output / "results.gpkg", layer="sites")
    assert set(sites.site_id) == ids
    assert sites.status.eq("likely_access_island").all()


def test_tiny_site_with_consistent_mapped_access_retains_access_evidence(tmp_path):
    sites = statuses(
        tmp_path,
        [box(0, 0, 1, 1)],
        [
            (LineString([(-20, -10), (50, -10)]), "inferred", True, "osm:1", "osm:2"),
            (LineString([(0.5, -10), (0.5, 0.5)]), "official", False),
        ],
    )
    assert sites.status.iloc[0] == "access_evidenced"
    assert "very_small_site_geometry" in sites.reasons.iloc[0]


@pytest.mark.parametrize(
    "tier,expected",
    [("excluded", "likely_access_island"), ("permissive", "permissive_access_evidenced"), ("unknown", "uncertain")],
)
def test_track_does_not_prove_access(tmp_path, tier, expected):
    sites = statuses(
        tmp_path,
        [box(0, 0, 10, 10)],
        [
            (LineString([(-20, -10), (50, -10)]), "inferred", True, "osm:1", "osm:2"),
            (LineString([(5, -10), (5, 5)]), tier, False, "osm:1", "osm:3"),
        ],
    )
    assert sites.status.iloc[0] == expected


def test_tolerance_and_missing_coverage(tmp_path):
    sites = statuses(
        tmp_path,
        [box(0, 0, 10, 10)],
        [
            (LineString([(-20, -10), (50, -10)]), "inferred", True, "osm:1", "osm:2"),
            (LineString([(5, -10), (5, -1)]), "official", False),
        ],
    )
    assert sites.status.iloc[0] == "uncertain"
    assert "tolerance_sensitive" in sites.reasons.iloc[0]


def test_missing_coverage_is_uncertain(tmp_path):
    sites = statuses(
        tmp_path,
        [box(0, 0, 10, 10)],
        [
            (LineString([(-20, -100), (50, -100)]), "inferred", True, "osm:1", "osm:2"),
        ],
        missing=True,
    )
    assert sites.status.iloc[0] == "uncertain"
    assert "official_coverage_missing" in sites.reasons.iloc[0]


def test_road_along_boundary_requires_entrance(tmp_path):
    sites = statuses(
        tmp_path,
        [box(0, 0, 10, 10)],
        [
            (LineString([(-20, 0), (50, 0)]), "inferred", True, "osm:1", "osm:2"),
        ],
    )
    assert sites.status.iloc[0] == "uncertain"
    assert "boundary_contact_without_entrance" in sites.reasons.iloc[0]


def test_blocking_barrier_does_not_establish_access(tmp_path):
    root = fixture_inputs(
        tmp_path,
        [box(0, 0, 10, 10)],
        [
            (LineString([(-20, -10), (5, -10)]), "inferred", True, "osm:1", "osm:2"),
            (LineString([(5, -10), (5, -1)]), "public", False, "osm:2", "osm:3"),
            (LineString([(5, -1), (5, 5)]), "public", False, "osm:3", "osm:4"),
        ],
    )
    barriers = gpd.GeoDataFrame(
        [{"node_id": "3", "blocking": True, "tags": "{}", "geometry": Point(5, -1)}], crs=CRS
    )
    write_layer(barriers, root / "prepared" / "barriers.gpkg", "barriers")
    output = tmp_path / "out"
    analyse({}, root, output)
    sites = gpd.read_file(output / "results.gpkg", layer="sites")
    assert sites.status.iloc[0] == "uncertain"
    assert "mapped_barrier_at_contact" in sites.reasons.iloc[0]


def test_osm_geometric_crossing_does_not_connect(tmp_path):
    sites = statuses(
        tmp_path,
        [box(0, 0, 10, 10)],
        [
            (LineString([(-20, -10), (50, -10)]), "inferred", True, "osm:1", "osm:2"),
            (LineString([(5, -20), (5, 5)]), "public", False, "osm:3", "osm:4"),
        ],
    )
    assert sites.status.iloc[0] == "likely_access_island"


@pytest.mark.parametrize("grade", ["1:yes:no", "-1:no:yes"])
@pytest.mark.parametrize("official_shadow", [False, True])
def test_bridge_or_tunnel_does_not_prove_surface_entry(tmp_path, grade, official_shadow):
    routes = [(LineString([(-20, 5), (50, 5)]), "inferred", True, "osm:1", "osm:2", grade)]
    if official_shadow:
        routes.append((LineString([(-20, 5), (50, 5)]), "official", False))
    sites = statuses(tmp_path, [box(0, 0, 10, 10)], routes, tile_size=100)
    assert sites.status.iloc[0] == "uncertain"
    assert "possible_grade_separation_at_contact" in sites.reasons.iloc[0]


def test_surface_approach_still_proves_access_near_a_bridge(tmp_path):
    sites = statuses(
        tmp_path,
        [box(0, 0, 10, 10)],
        [
            (LineString([(-20, 5), (50, 5)]), "inferred", True, "osm:1", "osm:2", "1:yes:no"),
            (LineString([(-20, -10), (5, -10)]), "inferred", True, "osm:3", "osm:4"),
            (LineString([(5, -10), (5, 5)]), "public", False, "osm:4", "osm:5"),
        ],
    )
    assert sites.status.iloc[0] == "access_evidenced"


def test_tiled_analysis_is_identical_and_resumes(tmp_path):
    root = fixture_inputs(
        tmp_path,
        [box(95, 95, 105, 105)],
        [
            (LineString([(50, 80), (150, 80)]), "inferred", True, "osm:1", "osm:2"),
            (LineString([(100, 80), (100, 100)]), "official", False),
        ],
    )
    for size in [100, 20000]:
        analyse({"analysis": {"tile_size_m": size}}, root, tmp_path / str(size))
    a = gpd.read_file(tmp_path / "100" / "results.gpkg", layer="sites")
    b = gpd.read_file(tmp_path / "20000" / "results.gpkg", layer="sites")
    assert a.drop(columns="geometry").equals(b.drop(columns="geometry"))
    manifest = analyse({"analysis": {"tile_size_m": 100}}, root, tmp_path / "100")
    assert manifest["complete"]
    assert a.geometry.iloc[0].contains(a.geometry.representative_point().iloc[0])


def test_corner_contact_keeps_sites_separate():
    land = gpd.GeoDataFrame(
        [
            {"component_id": str(i), "source_id": str(i), "name": "", "geometry": g}
            for i, g in enumerate([box(0, 0, 10, 10), box(10, 10, 20, 20)])
        ],
        crs=CRS,
    )
    sites, membership = form_sites(land)
    assert len(sites) == len(membership) == 2


def test_nearest_route_uses_polygon_extent_and_preserves_search_limit(tmp_path):
    # A large site extends beyond its representative point's tile. Private
    # routes still count as mapped evidence, and distant routes remain blank.
    polygons = [box(0, 0, 50000, 10), box(100000, 0, 100010, 10)]
    root = fixture_inputs(
        tmp_path,
        polygons,
        [
            (LineString([(50030, 0), (50030, 10)]), "excluded", False),
            (LineString([(107010, 0), (107010, 10)]), "public", False),
        ],
    )
    sites = gpd.GeoDataFrame(geometry=polygons, crs=CRS)
    add_nearest_distance(sites, root / "prepared" / "routes.gpkg")
    assert sites.nearest_mapped_route_m.iloc[0] == 30
    assert sites.nearest_mapped_route_m.isna().iloc[1]


def test_multipart_explodes_and_bad_geometry_is_accounted(tmp_path):
    from access_islands.prepare import normalise_land

    land = gpd.GeoDataFrame(
        [
            {"id": 1, "geometry": MultiPolygon([box(0, 0, 10, 10), box(100, 0, 110, 10)])},
            {"id": 2, "geometry": None},
        ],
        crs=CRS,
    )
    p = tmp_path / "land.gpkg"
    write_layer(land, p, "land")
    frame, accounting, rejected = normalise_land(
        {role: {"path": str(p), "id_field": "id"} for role in ["land", "dedicated"]},
        box(-100, -100, 200, 200),
    )
    assert len(frame) == 4
    assert len(rejected) == 2
    assert sum(row["outcome"] == "processed" for row in accounting) == 4
