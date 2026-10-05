"""CSV/GIS exports and a local, partitioned Leaflet map."""

from __future__ import annotations

import json
import logging
import os
import shutil
from pathlib import Path

import pandas as pd
import pyogrio

from .common import atomic_json, read_json

LOG = logging.getLogger(__name__)
ASSETS = {
    "leaflet.js": "https://unpkg.com/leaflet@1.9.4/dist/leaflet.js",
    "leaflet.css": "https://unpkg.com/leaflet@1.9.4/dist/leaflet.css",
    "markercluster.js": "https://unpkg.com/leaflet.markercluster@1.5.3/dist/leaflet.markercluster.js",
    "markercluster.css": "https://unpkg.com/leaflet.markercluster@1.5.3/dist/MarkerCluster.css",
    "markercluster-default.css": "https://unpkg.com/leaflet.markercluster@1.5.3/dist/MarkerCluster.Default.css",
}


def js_json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False).replace("</", "<\\/")


def atomic_text(path, value):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(value, encoding="utf-8")
    os.replace(tmp, path)


def export(output: Path, assets_root: Path | None = None):
    manifest = read_json(output / "analysis.json")
    if not manifest or not (output / "results.gpkg").exists():
        raise ValueError("Run analyse first")
    frame = pyogrio.read_dataframe(output / "results.gpkg", layer="sites")
    table = frame.drop(columns="geometry")
    # Deterministic examples support review without claiming a definitive-map or field inspection.
    samples = [
        group.sort_values(["area_ha", "site_id"], ascending=[False, True]).head(10)
        for _, group in table.groupby("status", sort=True)
    ]
    if samples:
        review = pd.concat(samples).copy()
    else:
        review = table.copy()
    review["review_outcome"] = ""
    review["review_notes"] = ""
    review["review_source"] = ""
    review["review_date"] = ""
    previous_review = output / "review_sample.csv"
    if previous_review.exists():
        previous = pd.read_csv(previous_review).drop_duplicates("site_id").set_index("site_id")
        compatible = previous.index.isin(table.site_id)
        if "geometry_sha256" in previous and "geometry_sha256" in table:
            hashes = table.set_index("site_id").geometry_sha256
            compatible &= previous.geometry_sha256.eq(previous.index.map(hashes))
        lost = previous.loc[~compatible].reset_index()
        previous = previous.loc[compatible]
        reviewed_ids = previous.index[
            previous.review_outcome.fillna("").ne("") | previous.review_notes.fillna("").ne("")
        ]
        review = pd.concat([review, table.loc[table.site_id.isin(reviewed_ids)]]).drop_duplicates("site_id")
        for field in ["review_outcome", "review_notes", "review_source", "review_date"]:
            if field in previous:
                review[field] = review.site_id.map(previous[field]).fillna("")
        if len(lost):
            history = output / "review_history.csv"
            old = pd.read_csv(history) if history.exists() else pd.DataFrame()
            pd.concat([old, lost]).drop_duplicates().to_csv(history, index=False)
    review["map_link"] = review.site_id.map(lambda sid: f"map/index.html?site={sid}")
    review.to_csv(output / "review_sample.csv", index=False, encoding="utf-8-sig")
    for filename, selection in [
        ("sites.csv", table),
        ("likely_access_islands.csv", table.loc[table.status == "likely_access_island"]),
        ("permissive_access_evidenced.csv", table.loc[table.status == "permissive_access_evidenced"]),
    ]:
        tmp = output / (filename + ".tmp")
        selection.to_csv(tmp, index=False, encoding="utf-8-sig", float_format="%.17g")
        os.replace(tmp, output / filename)
    candidates = frame.loc[frame.status == "likely_access_island"].to_crs(4326)
    atomic_text(output / "likely_access_islands.geojson", candidates.to_json(drop_id=True))
    map_dir = output / "map"
    assets = map_dir / "assets"
    partitions = map_dir / "polygons"
    assets.mkdir(parents=True, exist_ok=True)
    partitions.mkdir(parents=True, exist_ok=True)
    if assets_root is None:
        assets_root = Path(__file__).with_name("assets")
    for name in ASSETS:
        shutil.copyfile(assets_root / name, assets / name)
    for licence in assets_root.glob("*-LICENSE.txt"):
        shutil.copyfile(licence, assets / licence.name)
    # All marker metadata is compact. Geometry is loaded by partition only when requested.
    partition_ids = (
        frame.easting.floordiv(50000).astype(int).astype(str)
        + "_"
        + frame.northing.floordiv(50000).astype(int).astype(str)
    )
    markers = []
    bounds = frame.to_crs(4326).geometry.bounds
    for (_, row), partition, extent in zip(frame.iterrows(), partition_ids, bounds.itertuples(index=False)):
        markers.append(
            {
                "id": row.site_id,
                "name": row["name"],
                "status": row.status,
                "lat": float(row.latitude),
                "lon": float(row.longitude),
                "county": row.county,
                "council": row.council,
                "counties": sorted(set(filter(None, [row.county, *row.all_counties.split(";")]))),
                "councils": sorted(set(filter(None, [row.council, *row.all_councils.split(";")]))),
                "bounds": [[extent.miny, extent.minx], [extent.maxy, extent.maxx]],
                "area": round(float(row.area_ha), 3),
                "reasons": row.reasons,
                "evidence": row.evidence_strength,
                "regimes": row.access_regimes,
                "registeredCommon": bool(row.registered_common_land),
                "unverifiedCommons": row.unverified_common_ids,
                "partition": partition,
            }
        )
    for partition in sorted(set(partition_ids)):
        subset = frame.loc[partition_ids == partition, ["site_id", "geometry"]].copy()
        subset["geometry"] = subset.geometry.simplify(2, preserve_topology=True)
        geojson = json.loads(subset.to_crs(4326).to_json(drop_id=True))
        atomic_text(
            partitions / f"{partition}.js",
            f"window.registerPolygons({js_json(partition)}, {js_json(geojson)});\n",
        )
    gaps = pyogrio.read_dataframe(output / "results.gpkg", layer="coverage_gaps").to_crs(4326)
    gaps["geometry"] = (
        gaps.to_crs(27700).geometry.simplify(100).set_crs(27700, allow_override=True).to_crs(4326)
    )
    layers = {row[0] for row in pyogrio.list_layers(output / "results.gpkg")}
    study_json = '{"type":"FeatureCollection","features":[]}'
    commons_json = '{"type":"FeatureCollection","features":[]}'
    if "unverified_commons" in layers:
        commons = pyogrio.read_dataframe(output / "results.gpkg", layer="unverified_commons")
        commons["geometry"] = commons.geometry.simplify(2, preserve_topology=True)
        commons_json = commons.to_crs(4326).to_json(drop_id=True)
    if "study_area" in layers:
        study = pyogrio.read_dataframe(output / "results.gpkg", layer="study_area")
        study["geometry"] = study.geometry.simplify(10, preserve_topology=True)
        study_json = study.to_crs(4326).to_json(drop_id=True)
    atomic_text(
        map_dir / "data.js",
        "window.siteData="
        + js_json(markers)
        + ";\nwindow.coverageData="
        + gaps.to_json(drop_id=True)
        + ";\nwindow.studyAreaData="
        + study_json
        + ";\nwindow.unverifiedCommons="
        + commons_json
        + ";\nwindow.runInfo="
        + js_json(manifest)
        + ";\n",
    )
    atomic_text(map_dir / "app.js", APP_JS)
    atomic_text(map_dir / "index.html", HTML)
    report = {
        **manifest,
        "map": "map/index.html",
        "csv": "sites.csv",
        "candidate_csv": "likely_access_islands.csv",
        "permissive_csv": "permissive_access_evidenced.csv",
        "gis": "results.gpkg",
        "nearest_route_search_limit_m": 6400,
        "basemap": "Optional OpenStreetMap tiles require internet; site data and map scripts are local.",
    }
    atomic_json(output / "run_manifest.json", report)
    atomic_text(output / "quality_report.md", quality_report(report))
    LOG.info("Exported %d sites. Open %s", len(frame), map_dir / "index.html")
    return report


def quality_report(report):
    coverage = report.get("coverage", {})
    missing = coverage.get("missing_authorities", [])
    lines = [
        "# Access-island screening report",
        "",
        f"Run complete: **{report['complete']}**",
        f"Sites: {report['sites']}; land components: {report['land_components']}; route segments: {report['route_segments']}",
        "",
        "## Classification counts",
        "",
    ]
    lines.extend(f"- {status}: {count}" for status, count in sorted(report["counts"].items()))
    lines.extend(
        [
            "",
            "## Interpretation",
            "",
            report["interpretation"],
            "",
            "Ordinary roads are inferred pedestrian network anchors, not certified public-highway records. "
            "Permissive routes, unknown access, barriers and mapping tolerance affect uncertainty. "
            "Temporary closures and unmapped physical obstacles are not modelled.",
            "Permissive access evidenced requires a strong mapped connection at all three tolerances. Permission may be withdrawn; it does not establish a public right of way. "
            "Registered commons and section 15 land are statutory study sites; OSM common tags are unverified context and can only support uncertainty. "
            "Whole-site traversability, internal fences and terrain have not been verified.",
            "",
            "Blank nearest-route distances mean no route was found within 6.4 km; distances are straight-line distances.",
            "",
            f"Rejected source components: {report['rejected_components']}. See component_accounting.json.",
            "",
            f"Official rights-of-way coverage is missing for {len(missing)} authorities. "
            "Affected sites carry an explicit coverage flag; mapped OSM connections can still supply evidence.",
            "Missing authorities: " + (", ".join(row["name"] for row in missing) or "none reported") + ".",
            "Coverage documentation: " + str(coverage.get("url", coverage.get("path", "local input"))) + ".",
            "",
            "## Sources",
            "",
        ]
    )
    for role, source in report["sources"].items():
        lines.append(
            f"- {role}: {source.get('url', source.get('path'))}; retrieved {source.get('retrieved')}; "
            f"version {source.get('version', source.get('last_modified'))}; SHA256 {source.get('sha256')}"
        )
        attribution = source.get("attribution", "")
        if isinstance(attribution, dict):
            attribution = attribution.get("attributionStatement", attribution.get("text", ""))
        if attribution:
            lines.append(f"  Attribution: {attribution}")
    lines.extend(
        [
            "",
            "## Validation",
            "",
            "Automated tests cover geometry, access policy, connectivity, tiling, acquisition and exports. "
            "This report does not imply a manual inspection or field survey has been performed.",
            "",
        ]
    )
    return "\n".join(lines)


HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>England access islands</title><link rel="stylesheet" href="assets/leaflet.css">
<link rel="stylesheet" href="assets/markercluster.css"><link rel="stylesheet" href="assets/markercluster-default.css">
<style>body{margin:0;font:15px system-ui;color:#243b36}header{padding:12px 20px;background:#f1f5ed}h1{font-size:22px;margin:0 0 6px}
#controls{display:flex;gap:12px;flex-wrap:wrap;margin-top:10px}input,select{padding:7px;max-width:220px}#map{height:calc(100vh - 190px);min-height:350px}
.dot{border-radius:100%;border:2px solid white;box-shadow:0 0 3px #333}.legend{padding:10px;background:white;line-height:1.8}
.leaflet-control-layers-toggle{background-image:none!important;width:58px!important}
.leaflet-control-layers-toggle:after{content:'Layers';display:block;text-align:center;line-height:36px;font:12px/36px system-ui;color:#243b36}
#count{margin:8px 0 0}a{color:#135c48}#error{color:#a22}details{font-size:13px}label{display:inline-flex;align-items:center;gap:4px}</style></head>
<body><header><h1>England access islands</h1><div>Statutory access land and mapped walking approaches. Candidates need local review.</div>
<div id="controls"><input id="search" aria-label="Search sites" placeholder="Search site or ID">
<select id="county" aria-label="County"><option value="">All counties / unitary areas</option></select>
<select id="council" aria-label="Council"><option value="">All councils</option></select>
<label><input type="checkbox" name="status" value="likely_access_island" checked>Likely islands</label>
<label><input type="checkbox" name="status" value="uncertain" checked>Uncertain</label>
<label><input type="checkbox" name="status" value="permissive_access_evidenced" checked>Permissive access evidenced</label>
<label><input type="checkbox" name="status" value="access_evidenced">Public access evidenced</label></div>
<div id="count"></div><div id="error" role="alert"></div>
<details><summary>Downloads, sources and interpretation</summary><a href="../sites.csv">Complete CSV</a> ·
<a href="../likely_access_islands.csv">Candidate CSV</a> · <a href="../results.gpkg">GIS data</a> ·
<a href="../permissive_access_evidenced.csv">Permissive access CSV</a> ·
<a href="../quality_report.md">Quality report</a>. Click a site to load its boundary. At zoom 10+, visible sites load automatically.
Online basemap tiles are optional; all analysis data is local. Public access does not imply public ownership.</details></header>
<div id="map"></div><script src="assets/leaflet.js"></script><script src="assets/markercluster.js"></script>
<script src="data.js"></script><script src="app.js"></script></body></html>"""

APP_JS = """'use strict';
const colours={likely_access_island:'#c04c22',uncertain:'#b68a0c',access_evidenced:'#2d7861',permissive_access_evidenced:'#4978a8'};
if(window.runInfo.scope.synthetic)document.querySelector('h1').textContent='Synthetic demonstration — not real access islands';
const map=L.map('map',{preferCanvas:true}).setView([53,-2],6);
if(window.runInfo.scope.region&&!window.runInfo.scope.synthetic){
document.querySelector('h1').textContent=window.runInfo.scope.region.name+' access land';
document.title=window.runInfo.scope.region.name+' access land';}
const study=L.geoJSON(window.studyAreaData||{type:'FeatureCollection',features:[]},
 {style:{color:'#627471',weight:2,fillColor:'#f7f5ed',fillOpacity:.3}}).addTo(map);
if(study.getLayers().length)map.fitBounds(study.getBounds(),{padding:[15,15],animate:false});
map.attributionControl.addAttribution('Land / PRoW: Natural England, contains OS data; boundaries: ONS; routes / commons: © OpenStreetMap contributors, ODbL');
const base=L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png',{maxZoom:19,attribution:'© OpenStreetMap contributors'});
base.addTo(map);
const coverage=L.geoJSON(window.coverageData,{style:{color:'#745792',weight:1,fillOpacity:.12},onEachFeature:(f,l)=>l.bindPopup('Official rights-of-way data missing: '+escapeHtml(f.properties.name))});
const cluster=L.markerClusterGroup({chunkedLoading:false}).addTo(map);
const polygons=L.layerGroup().addTo(map);
const commons=L.geoJSON(window.unverifiedCommons||{type:'FeatureCollection',features:[]},
 {style:{color:'#745792',weight:2,dashArray:'5 5',fillOpacity:.08},onEachFeature:(f,l)=>l.bindPopup(
 'Unverified OSM common: '+escapeHtml(f.properties.name||f.properties.source_id)+
 '<br>foot: '+escapeHtml(f.properties.foot||'not recorded')+'; access: '+escapeHtml(f.properties.access||'not recorded')+
 '<br>This tag does not establish statutory access rights.')} );
L.control.layers({'Online basemap':base},{'Missing official route coverage':coverage,'Unverified OSM commons':commons}).addTo(map);
const loaded=new Map(),pending=new Map(); let visible=[];
function escapeHtml(s){return String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
function popup(s){return '<strong>'+escapeHtml(s.name)+'</strong><br>'+escapeHtml(s.id)+'<br>'+escapeHtml(s.status.replaceAll('_',' '))+
 '<br>'+s.area+' ha · '+escapeHtml(s.county)+'<br>Council: '+escapeHtml(s.council)+'<br>'+s.lat.toFixed(6)+', '+s.lon.toFixed(6)+
 '<br>Evidence: '+escapeHtml(s.evidence)+'<br>'+escapeHtml(s.reasons.replaceAll(';','; '))+
 '<br>Access regimes: '+escapeHtml((s.regimes||'').replaceAll(';','; '))+
 '<br>Registered common: '+(s.registeredCommon?'yes':'not recorded')+
 (s.unverifiedCommons?'<br>Nearby unverified OSM commons: '+escapeHtml(s.unverifiedCommons):'')+
 '<br><a target="_blank" rel="noopener" href="https://www.openstreetmap.org/?mlat='+s.lat+'&mlon='+s.lon+'#map=16/'+s.lat+'/'+s.lon+'">View location on OSM</a>';}
window.registerPolygons=(key,data)=>{loaded.set(key,data); const p=pending.get(key);if(p){p.resolve(data);pending.delete(key);}};
function load(key){if(loaded.has(key))return Promise.resolve(loaded.get(key));if(pending.has(key))return pending.get(key).promise;
 let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});pending.set(key,{promise,resolve,reject});
 const script=document.createElement('script');script.src='polygons/'+encodeURIComponent(key)+'.js';
 script.onerror=()=>{pending.delete(key);reject(new Error('Cannot load local polygon partition '+key));};document.body.append(script);return promise;}
async function showSite(s,fit=false){try{const data=await load(s.partition);const feature=data.features.find(f=>f.properties.site_id===s.id);if(!feature)return;
 const layer=L.geoJSON(feature,{style:{color:colours[s.status],weight:2,fillOpacity:.2}}).bindPopup(popup(s));
 if(fit){map.fitBounds(layer.getBounds(),{maxZoom:16,animate:false});await showVisible();
 const shown=polygons.getLayers().find(l=>l.getLayers()[0]?.feature?.properties.site_id===s.id);
 if(shown)shown.openPopup();else{polygons.addLayer(layer);layer.openPopup();}}
 else polygons.addLayer(layer);}catch(e){document.querySelector('#error').textContent=e.message;}}
function fill(id,values){const select=document.getElementById(id);for(const value of [...new Set(values)].filter(Boolean).sort()){
 const option=document.createElement('option');option.value=value;option.textContent=value;select.append(option);}}
fill('county',window.siteData.flatMap(s=>s.counties));fill('council',window.siteData.flatMap(s=>s.councils));
let renderVersion=0,markerVersion=0;
async function showVisible(){const version=++renderVersion;polygons.clearLayers();if(map.getZoom()<10)return;
 const inView=visible.filter(s=>map.getBounds().intersects(L.latLngBounds(s.bounds)));
 await Promise.all([...new Set(inView.map(s=>s.partition))].map(k=>load(k).catch(e=>{document.querySelector('#error').textContent=e.message;return null;})));
 if(version!==renderVersion)return;await Promise.all(inView.map(s=>showSite(s)));}
function render(){const text=document.getElementById('search').value.toLowerCase();const county=document.getElementById('county').value;
 const council=document.getElementById('council').value;const statuses=new Set([...document.querySelectorAll('input[name=status]:checked')].map(e=>e.value));
 visible=window.siteData.filter(s=>statuses.has(s.status)&&(!county||s.counties.includes(county))&&(!council||s.councils.includes(council))&&
 (!text||(s.name+' '+s.id).toLowerCase().includes(text)));
 const version=++markerVersion;cluster.clearLayers();
 const markers=visible.map(s=>{const marker=L.marker([s.lat,s.lon],{icon:L.divIcon({className:'dot',html:'',iconSize:[12,12],
 iconAnchor:[6,6]})}).bindPopup(popup(s));marker.on('add',()=>{if(marker.getElement())marker.getElement().style.backgroundColor=colours[s.status];});
 marker.on('click',()=>showSite(s,true));return marker;});
 let offset=0;function addChunk(){if(version!==markerVersion)return;
 cluster.addLayers(markers.slice(offset,offset+500));offset+=500;
 if(offset<markers.length)setTimeout(addChunk,0);}addChunk();
 document.getElementById('count').textContent=visible.length.toLocaleString()+' shown / '+window.siteData.length.toLocaleString()+' sites. '+
 (window.runInfo.complete?'All source components accounted for.':'Partial run: inspect rejected components.');showVisible();}
let timer;document.getElementById('controls').addEventListener('input',()=>{clearTimeout(timer);timer=setTimeout(render,180);});
map.on('moveend',showVisible);render();
const requested=new URLSearchParams(location.search).get('site');
if(requested){const site=window.siteData.find(s=>s.id===requested);if(site){document.getElementById('search').value=requested;
document.querySelector('input[value="'+site.status+'"]').checked=true;render();showSite(site,true);}}
const legend=L.control({position:'bottomright'});legend.onAdd=()=>{const div=L.DomUtil.create('div','legend');div.innerHTML=
 '<span style="color:#c04c22">●</span> Likely access island<br><span style="color:#b68a0c">●</span> Uncertain<br><span style="color:#4978a8">●</span> Permissive access evidenced<br><span style="color:#2d7861">●</span> Public access evidenced';return div;};legend.addTo(map);
"""
