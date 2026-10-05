import json

import geopandas as gpd
import pandas as pd
from shapely.geometry import LineString, box
from test_analysis import fixture_inputs

from access_islands.analyse import analyse
from access_islands.export import ASSETS, export


def test_exports_counts_ids_and_local_partitions(tmp_path):
    root = fixture_inputs(
        tmp_path,
        [box(0, 0, 10, 10)],
        [
            (LineString([(-20, -100), (50, -100)]), "inferred", True, "osm:1", "osm:2"),
        ],
    )
    output = tmp_path / "out"
    analyse({}, root, output)
    # Use real bundled assets in this case: export must not need a network request.
    report = export(output)
    csv = pd.read_csv(output / "sites.csv")
    gis = gpd.read_file(output / "results.gpkg", layer="sites")
    candidates = json.loads((output / "likely_access_islands.geojson").read_text())
    assert report["complete"]
    assert set(csv.site_id) == set(gis.site_id)
    assert set(pd.read_csv(output / "site_membership.csv").site_id) == set(csv.site_id)
    assert len(candidates["features"]) == len(csv) == 1
    assert list((output / "map" / "polygons").glob("*.js"))
    assert "registerPolygons" in next((output / "map" / "polygons").glob("*.js")).read_text()
    assert "leaflet.js" in (output / "map" / "index.html").read_text()
    assert (output / "map" / "assets" / "LEAFLET-LICENSE.txt").exists()
    assert (output / "map" / "assets" / "MARKERCLUSTER-LICENSE.txt").exists()


def test_empty_regional_output_has_valid_files(tmp_path):
    root = fixture_inputs(
        tmp_path,
        [box(0, 0, 10, 10)],
        [
            (LineString([(-20, -100), (50, -100)]), "inferred", True, "osm:1", "osm:2"),
        ],
    )
    output = tmp_path / "out"
    report = analyse({}, root, output, bbox=[1000, 1000, 1100, 1100])
    assert report["sites"] == 0
    assert pd.read_csv(output / "site_membership.csv").empty
    assert gpd.read_file(output / "results.gpkg", layer="sites").empty
    assets = tmp_path / "assets"
    assets.mkdir()
    for name in ASSETS:
        (assets / name).write_text("/* local test asset */", encoding="utf-8")
    export(output, assets)
    assert pd.read_csv(output / "sites.csv").empty
    assert json.loads((output / "likely_access_islands.geojson").read_text())["features"] == []
