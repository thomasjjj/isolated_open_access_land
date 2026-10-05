from pathlib import Path

import geopandas as gpd
import pandas as pd
import pytest
from shapely.geometry import LineString, box
from test_analysis import fixture_inputs

from access_islands.common import CRS, read_json, write_layer
from access_islands.regions import Progress, build_catalogue, prepare_region, research, resolve_region
from access_islands.reports import county_report


def test_reference_command_forwards_explicit_refresh(tmp_path, monkeypatch):
    from access_islands.cli import main

    calls = []
    monkeypatch.setattr(
        "access_islands.regions.ensure_reference",
        lambda config, root, refresh=False: calls.append((root, refresh)),
    )
    assert main(["reference", "--data-dir", str(tmp_path), "--refresh"]) == 0
    assert calls == [(tmp_path.resolve(), True)]


def test_regional_inputs_exclude_distant_routes_and_reuse_cache(tmp_path):
    root = fixture_inputs(
        tmp_path,
        [box(0, 0, 10, 10)],
        [
            (LineString([(-20, -100), (50, -100)]), "inferred", True, "osm:1", "osm:2"),
            (LineString([(100000, 0), (100100, 0)]), "inferred", True, "osm:3", "osm:4"),
        ],
    )
    spec = resolve_region(root, {}, county=["Test area"])
    progress = Progress(tmp_path / "progress.json")
    local, _ = prepare_region({}, root, spec, 100, progress)
    manifest = read_json(local / "prepared" / "manifest.json")
    assert manifest["route_count"] == 1
    assert manifest["catalogue"]
    before = (local / "prepared" / "routes.gpkg").stat().st_mtime_ns
    assert prepare_region({}, root, spec, 100, progress)[0] == local
    assert (local / "prepared" / "routes.gpkg").stat().st_mtime_ns == before


def test_cross_border_site_retains_complete_geometry_and_id(tmp_path):
    root = fixture_inputs(
        tmp_path,
        [box(0, 0, 10, 10), box(10, 0, 20, 10)],
        [(LineString([(-20, -100), (50, -100)]), "inferred", True, "osm:1", "osm:2")],
    )
    for role in ["counties", "councils"]:
        write_layer(
            gpd.GeoDataFrame(
                [{"code": "E00000001", "name": "Test area", "geometry": box(0, -20, 10, 20)}], crs=CRS
            ),
            root / "prepared" / "land.gpkg",
            role,
        )
    catalogue, _ = build_catalogue(root)
    canonical = gpd.read_file(catalogue / "sites.gpkg", layer="sites")
    spec = resolve_region(root, {}, county=["E00000001"])
    local, _ = prepare_region({}, root, spec, 100, Progress(tmp_path / "progress.json"))
    sites = gpd.read_file(local / "prepared" / "land.gpkg", layer="sites")
    assert sites.site_id.tolist() == canonical.site_id.tolist()
    assert sites.geometry.iloc[0].area == 200
    assert len(pd.read_csv(local / "prepared" / "membership.csv")) == 2


@pytest.mark.parametrize("maximum,expected", [(100, "uncertain"), (4000, "access_evidenced")])
def test_context_edge_is_uncertain_and_expansion_finds_external_anchor(tmp_path, maximum, expected):
    root = fixture_inputs(
        tmp_path,
        [box(0, 0, 10, 10)],
        [
            (LineString([(-3000, -10), (5, -10), (5, 5)]), "public", False, "osm:1", "osm:2"),
            (LineString([(-3100, -10), (-3000, -10)]), "inferred", True, "osm:3", "osm:1"),
        ],
    )
    spec = resolve_region(root, {}, county=["Test area"])
    config = {"analysis": {"context_buffer_m": 100, "max_context_buffer_m": maximum}}
    result = research(config, root, tmp_path / "outputs", tmp_path / "reports", spec)
    output = tmp_path / "outputs" / spec["slug"]
    sites = gpd.read_file(output / "results.gpkg", layer="sites")
    assert sites.status.tolist() == [expected]
    if expected == "uncertain":
        assert "regional_context_incomplete" in sites.reasons.iloc[0]
    assert result["complete"]
    report = Path(result["county_report"])
    assert report.name == "Test area.md"
    assert "# Test area" in report.read_text(encoding="utf-8")
    assert "maps/test-area-overview.png" in report.read_text(encoding="utf-8")
    assert (report.parent / "maps" / "test-area-overview.png").stat().st_size > 1000
    assert (report.parent / "maps" / "test-area-candidates.png").stat().st_size > 1000
    assert read_json(output / "progress.json")["complete"]
    assert read_json(report.parent / "index.json")["test-area"]["sites"] == 1


def test_report_rejects_incomplete_or_unvalidated_outputs(tmp_path):
    with pytest.raises((ValueError, TypeError)):
        county_report(tmp_path, tmp_path / "reports")
    assert not (tmp_path / "reports").exists()


def test_review_notes_survive_reexport(tmp_path):
    from access_islands.export import export

    root = fixture_inputs(
        tmp_path,
        [box(0, 0, 10, 10)],
        [(LineString([(-20, -100), (50, -100)]), "inferred", True, "osm:1", "osm:2")],
    )
    spec = resolve_region(root, {}, county=["Test area"])
    research(
        {"analysis": {"context_buffer_m": 100, "max_context_buffer_m": 100}},
        root,
        tmp_path / "out",
        tmp_path / "reports",
        spec,
    )
    output = tmp_path / "out" / spec["slug"]
    review = pd.read_csv(output / "review_sample.csv")
    review["review_outcome"] = "needs entrance inspection"
    review["review_notes"] = "Council map checked"
    review.to_csv(output / "review_sample.csv", index=False)
    export(output)
    saved = pd.read_csv(output / "review_sample.csv")
    assert saved.review_notes.iloc[0] == "Council map checked"
    assert saved.review_outcome.iloc[0] == "needs entrance inspection"
