from xml.etree import ElementTree

import geopandas as gpd
import pyogrio
from shapely.geometry import box

from access_islands.cartography import extract_basemap, load_context, plot_map
from access_islands.common import CRS, atomic_json
from access_islands.reports import preserve_previous_report


def test_offline_basemap_preserves_context_and_reuses_extract(tmp_path):
    osm = tmp_path / "local.osm"
    osm.write_text(
        """<osm version="0.6">
        <node id="1" lat="50.7" lon="-1.3"><tag k="place" v="town"/><tag k="name" v="Local town"/></node>
        <node id="2" lat="50.7" lon="-1.29"/>
        <node id="3" lat="50.71" lon="-1.29"/>
        <node id="4" lat="50.71" lon="-1.3"/>
        <way id="10"><nd ref="1"/><nd ref="2"/><tag k="highway" v="footway"/><tag k="access" v="private"/></way>
        <way id="11"><nd ref="1"/><nd ref="2"/><nd ref="3"/><nd ref="4"/><nd ref="1"/><tag k="natural" v="water"/></way>
        <way id="12"><nd ref="1"/><nd ref="2"/><nd ref="3"/><nd ref="4"/><nd ref="1"/></way>
        <relation id="20"><member type="way" ref="12" role="outer"/><tag k="type" v="multipolygon"/><tag k="landuse" v="forest"/></relation>
        </osm>""",
        encoding="utf-8",
    )
    path, meta = extract_basemap(osm, tmp_path / "cartography")
    assert set(pyogrio.read_dataframe(path, layer="areas").kind) == {"water", "woodland"}
    assert pyogrio.read_dataframe(path, layer="lines").kind.tolist() == ["path"]
    assert pyogrio.read_dataframe(path, layer="places").name.tolist() == ["Local town"]
    assert pyogrio.read_dataframe(path, layer="areas").geometry.is_valid.all()
    assert "do not establish access rights" in meta["purpose"]
    before = path.stat().st_mtime_ns
    assert extract_basemap(osm, tmp_path / "cartography")[1] == meta
    assert path.stat().st_mtime_ns == before


def test_outline_fallback_and_shareable_map_with_empty_inventory(tmp_path):
    study = gpd.GeoDataFrame(geometry=[box(430000, 80000, 450000, 95000)], crs=CRS)
    sites = gpd.GeoDataFrame(
        {"site_id": [], "status": [], "area_ha": [], "reasons": []}, geometry=[], crs=CRS
    )
    context = load_context({}, study, sites)
    assert not context["metadata"]["available"]
    target = tmp_path / "map.png"
    plot_map(sites, study, target, "Empty county", candidates_only=True, context=context)
    assert target.stat().st_size > 1000
    svg = target.with_suffix(".svg")
    ElementTree.parse(svg)
    text = svg.read_text(encoding="utf-8")
    assert "OpenStreetMap contributors" in text
    assert "No candidates identified" in text
    assert "easting" not in text.lower() and "northing" not in text.lower()


def test_visual_refresh_archives_previous_report_without_changing_analysis(tmp_path):
    reports = tmp_path / "reports"
    maps = reports / "maps"
    maps.mkdir(parents=True)
    county = tmp_path / "outputs" / "county"
    package = county / "runs" / "analysis-one"
    package.mkdir(parents=True)
    (package / "sites.csv").write_text("unchanged analysis", encoding="utf-8")
    (maps / "county-overview.png").write_bytes(b"old image")
    report = reports / "County.md"
    report.write_text(
        "![map](maps/county-overview.png)\n[CSV](../outputs/county/runs/analysis-one/sites.csv)\n",
        encoding="utf-8",
    )
    atomic_json(reports / "index.json", {"county": {"analysis_signature": "analysis-one"}})
    preserve_previous_report(reports, report, "county", "analysis-one", county, "new-visuals")
    archived = reports / "history" / "county" / "analysis-one" / "presentation-legacy" / "County.md"
    assert archived.exists()
    assert (archived.parent / "maps" / "county-overview.png").read_bytes() == b"old image"
    assert (package / "sites.csv").read_text(encoding="utf-8") == "unchanged analysis"
    assert "maps/county-overview.png" in archived.read_text(encoding="utf-8")
    assert (report).exists()
