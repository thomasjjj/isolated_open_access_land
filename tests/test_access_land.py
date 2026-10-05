import json

import geopandas as gpd
import pandas as pd
import pytest
from shapely.geometry import LineString, box
from test_analysis import fixture_inputs

from access_islands.access_land import (
    annotate_sites,
    common_tag,
    extend_sites,
    normalise_section15,
    reconcile_ids,
)
from access_islands.analyse import analyse, form_sites
from access_islands.common import CRS, atomic_json, read_json, write_layer
from access_islands.packages import preserve_completed, publish_completed


@pytest.mark.parametrize(
    "public,expected", [(False, "permissive_access_evidenced"), (True, "access_evidenced")]
)
def test_public_connection_wins_over_permissive(tmp_path, public, expected):
    routes = [
        (LineString([(-20, -10), (50, -10)]), "inferred", True, "osm:1", "osm:2"),
        (LineString([(5, -10), (5, 5)]), "permissive", False, "osm:1", "osm:3"),
    ]
    if public:
        routes.append((LineString([(6, -10), (6, 5)]), "official", False))
    root = fixture_inputs(tmp_path, [box(0, 0, 10, 10)], routes)
    analyse({}, root, tmp_path / "out")
    sites = gpd.read_file(tmp_path / "out/results.gpkg", layer="sites")
    assert sites.status.iloc[0] == expected
    assert sites.exact_permissive_access.iloc[0]


@pytest.mark.parametrize("gap,blocking", [(1, False), (0, True)])
def test_permissive_tolerance_or_barrier_remains_uncertain(tmp_path, gap, blocking):
    root = fixture_inputs(
        tmp_path,
        [box(0, 0, 10, 10)],
        [
            (LineString([(-20, -10), (50, -10)]), "inferred", True, "osm:1", "osm:2"),
            (LineString([(5, -10), (5, -gap if gap else 5)]), "permissive", False, "osm:1", "osm:3"),
        ],
    )
    if blocking:
        from shapely.geometry import Point

        write_layer(
            gpd.GeoDataFrame(
                [{"node_id": "3", "blocking": True, "tags": "{}", "geometry": Point(5, 0)}], crs=CRS
            ),
            root / "prepared/barriers.gpkg",
            "barriers",
        )
    analyse({}, root, tmp_path / "out")
    assert gpd.read_file(tmp_path / "out/results.gpkg", layer="sites").status.iloc[0] == "uncertain"


def test_unverified_common_connects_only_possible_graph(tmp_path):
    root = fixture_inputs(
        tmp_path,
        [box(0, 0, 10, 10)],
        [(LineString([(-20, -20), (50, -20)]), "inferred", True, "osm:1", "osm:2")],
    )
    hints = gpd.GeoDataFrame(
        [
            {
                "site_id": "hint:osm:way:1",
                "source_id": "osm:way:1",
                "name": "Test common",
                "access": "",
                "foot": "",
                "geometry": box(0, -25, 10, 0),
            }
        ],
        crs=CRS,
    )
    write_layer(hints, root / "prepared/land.gpkg", "unverified_commons")
    analyse({}, root, tmp_path / "out")
    sites = gpd.read_file(tmp_path / "out/results.gpkg", layer="sites")
    assert len(sites) == 1
    assert sites.status.iloc[0] == "uncertain"
    assert not sites.default_access.iloc[0]
    assert not sites.default_permissive_access.iloc[0]
    assert "unverified_osm_common_nearby" in sites.reasons.iloc[0]
    assert common_tag({"leisure": "common"})
    assert common_tag({"designation": "common"})
    assert common_tag({"landuse": "common"})


def test_section15_duplicate_references_accounting_union_and_lineage(tmp_path):
    source = tmp_path / "section15.gpkg"
    write_layer(
        gpd.GeoDataFrame(
            [
                {
                    "ref_no": "same",
                    "common": "First",
                    "of_act": "S.193",
                    "rcl_oc": "RCL",
                    "geometry": box(2, 2, 8, 8),
                },
                {
                    "ref_no": "same",
                    "common": "Second",
                    "of_act": "1899",
                    "rcl_oc": "RCL",
                    "geometry": box(20, 0, 30, 10),
                },
                {
                    "ref_no": "outside",
                    "common": "Outside",
                    "of_act": "Other",
                    "rcl_oc": "",
                    "geometry": box(200, 0, 210, 10),
                },
            ],
            crs=CRS,
        ),
        source,
        "land",
    )
    supplemental, accounting, rejects = normalise_section15({"path": str(source)}, box(-10, -10, 100, 100))
    assert len(supplemental) == 2 and supplemental.component_id.is_unique
    assert len(accounting) == 3 and accounting[-1]["outcome"] == "outside_england"
    assert rejects.empty
    old_land = gpd.GeoDataFrame(
        [
            {
                "component_id": "land:1",
                "source_id": "land:1",
                "name": "",
                "source_attributes": json.dumps({"rcl": "Yes", "oc": "No"}),
                "geometry": box(0, 0, 10, 10),
            }
        ],
        crs=CRS,
    )
    legacy, _ = form_sites(old_land)
    land = gpd.GeoDataFrame(pd.concat([old_land, supplemental], ignore_index=True), crs=CRS)
    sites, membership = form_sites(land)
    reconciled, replacements, lineage = reconcile_ids(sites, legacy)
    assert legacy.site_id.iloc[0] in set(reconciled.site_id)
    assert reconciled.geometry.area.sum() == 200  # the overlapping section 15 area counts once
    membership = [{**r, "site_id": replacements.get(r["site_id"], r["site_id"])} for r in membership]
    annotated = annotate_sites(reconciled, land, membership)
    assert annotated.registered_common_land.all()
    assert annotated.access_regimes.str.contains("section15_statutory_access").all()
    assert lineage[0]["relationship"] == "unchanged_geometry"
    extended = old_land.copy()
    extended.geometry = [box(0, 0, 20, 10)]
    changed, _ = form_sites(extended)
    # Geometry changes must never inherit an old ID, even if source membership IDs match.
    revised, _, history = reconcile_ids(changed, legacy)
    assert revised.site_id.iloc[0] != legacy.site_id.iloc[0]
    assert history[0]["relationship"] == "changed_geometry"


def test_failed_publication_preserves_completed_package(tmp_path):
    output, working = tmp_path / "county", tmp_path / "working"
    output.mkdir()
    working.mkdir()
    atomic_json(output / "run_manifest.json", {"signature": "old", "complete": True})
    atomic_json(output / "validation.json", {"analysis_signature": "old", "complete": True})
    (output / "sites.csv").write_text("old", encoding="utf-8")
    archive = preserve_completed(output)
    atomic_json(working / "run_manifest.json", {"signature": "new", "complete": False})
    atomic_json(working / "validation.json", {"analysis_signature": "new", "complete": True})
    with pytest.raises(ValueError):
        publish_completed(working, output)
    assert (output / "sites.csv").read_text() == "old"
    atomic_json(working / "run_manifest.json", {"signature": "new", "complete": True})
    (working / "sites.csv").write_text("new", encoding="utf-8")
    package = publish_completed(working, output)
    assert (archive / "sites.csv").read_text() == "old"
    assert (package / "sites.csv").read_text() == "new"
    assert read_json(output / "latest_completed.json")["signature"] == "new"


def test_incremental_catalogue_matches_full_grouping():
    def frame(polygons, prefix):
        return gpd.GeoDataFrame(
            [
                {"component_id": f"{prefix}:{i}", "source_id": f"{prefix}:{i}", "name": "", "geometry": g}
                for i, g in enumerate(polygons)
            ],
            crs=CRS,
        )

    old = frame([box(0, 0, 10, 10), box(10, 0, 20, 10), box(25, 0, 35, 10), box(50, 0, 60, 10)], "land")
    new = frame([box(19, 0, 26, 10), box(60, 10, 70, 20), box(100, 0, 110, 10)], "section15")
    land = gpd.GeoDataFrame(pd.concat([old, new], ignore_index=True), crs=CRS)
    legacy, membership = form_sites(old)
    incremental, members = extend_sites(legacy, pd.DataFrame(membership), new, land)
    full, full_members = form_sites(land)
    assert incremental.site_id.tolist() == full.site_id.tolist()
    assert all(a.equals(b) for a, b in zip(incremental.geometry, full.geometry))
    assert {(r["site_id"], r["component_id"]) for r in members} == {
        (r["site_id"], r["component_id"]) for r in full_members
    }


def test_largest_review_sample_keeps_previously_reviewed_small_site(tmp_path):
    from access_islands.export import export

    root = fixture_inputs(
        tmp_path,
        [box(i * 100, 0, i * 100 + 10 + i, 10) for i in range(21)],
        [(LineString([(-20, -100), (2500, -100)]), "inferred", True, "osm:1", "osm:2")],
    )
    output = tmp_path / "out"
    analyse({}, root, output)
    export(output)
    table = pd.read_csv(output / "sites.csv").sort_values("area_ha", ascending=False)
    selected = pd.read_csv(output / "review_sample.csv")
    assert selected.site_id.tolist() == table.head(10).site_id.tolist()
    small = table.tail(1).copy()
    small["review_outcome"] = "Council layer compared"
    small["review_notes"] = "Retain this review even though it is outside the largest sample"
    pd.concat([selected, small]).to_csv(output / "review_sample.csv", index=False)
    export(output)
    saved = pd.read_csv(output / "review_sample.csv")
    assert len(saved) == 11
    assert (
        saved.loc[saved.site_id.eq(small.site_id.iloc[0]), "review_outcome"].iloc[0]
        == "Council layer compared"
    )


def test_review_cannot_follow_a_changed_geometry_with_the_same_id(tmp_path):
    from access_islands.export import export

    root = fixture_inputs(
        tmp_path,
        [box(0, 0, 10, 10)],
        [(LineString([(-20, -100), (50, -100)]), "inferred", True, "osm:1", "osm:2")],
    )
    output = tmp_path / "out"
    analyse({}, root, output)
    export(output)
    review = pd.read_csv(output / "review_sample.csv")
    review["geometry_sha256"] = "different-older-geometry"
    review["review_outcome"] = "Earlier entrance checked"
    review.to_csv(output / "review_sample.csv", index=False)
    export(output)
    current = pd.read_csv(output / "review_sample.csv").fillna("")
    assert current.review_outcome.iloc[0] == ""
    assert pd.read_csv(output / "review_history.csv").review_outcome.iloc[0] == "Earlier entrance checked"
