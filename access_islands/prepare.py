"""Validated land, boundaries and streamed route normalisation."""

from __future__ import annotations

import collections
import inspect
import json
import logging
import math
import os
import shutil
import sqlite3
from pathlib import Path

import geopandas as gpd
import pandas as pd
import pyogrio
import shapely
from shapely.geometry import LineString, box

from .access import barrier_blocks, grade, pedestrian
from .common import (
    CRS,
    atomic_json,
    code_signature,
    digest,
    empty_frame,
    fingerprint,
    read_json,
    source_digest,
    write_layer,
)

LOG = logging.getLogger(__name__)
ROUTE_COLUMNS = [
    "route_index",
    "source_id",
    "tier",
    "seed",
    "start_node",
    "end_node",
    "grade",
    "reason",
    "name",
    "geometry",
]


def vector_source(spec: dict, role: str):
    path = Path(spec["path"])
    layer = spec.get("layer")
    if path.is_dir() and path.suffix.lower() != ".gdb":
        candidates = sorted(
            p for p in path.rglob("*") if p.suffix.lower() in {".gpkg", ".shp", ".gdb", ".geojson"}
        )
        if role == "prow":
            databases = [p for p in candidates if p.suffix.lower() in {".gpkg", ".gdb"}]
            candidates = databases or [
                p
                for p in candidates
                if "network" in p.name.lower()
                and not any(x in p.name.lower() for x in ["higher", "terrain", "density"])
            ]
        if len(candidates) != 1:
            raise ValueError(f"Choose a file for {role} using sources.{role}.path: {candidates}")
        path = candidates[0]
    layers = pyogrio.list_layers(path)
    if not layer:
        possible = [name for name, kind in layers if kind]
        if role == "prow" and len(possible) > 1:
            possible = [
                n
                for n in possible
                if "network" in n.lower()
                and not any(x in n.lower() for x in ["higher", "terrain", "density"])
            ]
        if len(possible) != 1:
            raise ValueError(f"Choose a layer for {role}: {list(layers[:, 0])}")
        layer = possible[0]
    return path, layer


def read_vector(spec, role):
    path, layer = vector_source(spec, role)
    # The NE GeoPackages contain curved surfaces. GDAL's ordinary reader strokes
    # those curves; Arrow transfers unsupported nonlinear WKB directly to Shapely.
    pyogrio.set_gdal_config_options({"OGR_ARC_STEPSIZE": "0.5", "OGR_ARC_MAX_GAP": "0.1"})
    frame = pyogrio.read_dataframe(path, layer=layer, use_arrow=role in {"countries", "counties", "councils"})
    if frame.crs is None:
        if not spec.get("crs"):
            raise ValueError(f"{role} has no CRS; supply one explicitly")
        frame = frame.set_crs(spec["crs"])
    if frame.empty:
        raise ValueError(f"Required source {role} is empty")
    return frame.to_crs(CRS)


def parts(geometry, types):
    if geometry is None or geometry.is_empty:
        return []
    if geometry.geom_type in types:
        return [geometry]
    if hasattr(geometry, "geoms"):
        return [p for g in geometry.geoms for p in parts(g, types)]
    return []


class CountryClipper:
    """Clip against small cached pieces instead of overlaying a two-million-point coastline for every parcel."""

    def __init__(self, country, tile_size=20000):
        self.country, self.tile_size, self.cache = country, tile_size, {}
        shapely.prepare(country)

    def clip(self, polygon):
        if shapely.covers(self.country, polygon):
            return polygon
        x0, y0, x1, y1 = polygon.bounds
        pieces = []
        size = self.tile_size
        for x in range(math.floor(x0 / size), math.floor(x1 / size) + 1):
            for y in range(math.floor(y0 / size), math.floor(y1 / size) + 1):
                key = (x, y)
                if key not in self.cache:
                    rectangle = box(x * size, y * size, (x + 1) * size, (y + 1) * size)
                    if shapely.covers(self.country, rectangle):
                        self.cache[key] = rectangle
                    else:
                        self.cache[key] = shapely.make_valid(shapely.clip_by_rect(self.country, *rectangle.bounds))
                pieces.append(self.cache[key])
        return polygon.intersection(shapely.union_all(pieces))


def normalise_land(sources, country):
    clipper = CountryClipper(country)
    records, accounting, rejected = [], [], []
    primary_includes_dedication = False
    for role in ["land", "dedicated"]:
        frame = read_vector(sources[role], role)
        spec = sources[role]
        if role == "land":
            primary_includes_dedication = spec.get("includes_section16", any(c.lower() == "s16" for c in frame.columns))
        id_col = spec.get("id_field") or next((c for c in frame if c.upper().startswith("OBJECTID")), None)
        for number, (_, row) in enumerate(frame.iterrows()):
            original = row.geometry
            source_id = f"{role}:{row[id_col] if id_col else number}"
            polygons = parts(shapely.make_valid(original) if original is not None else None, {"Polygon"})
            if not polygons:
                accounting.append({"source_id": source_id, "component": 0, "outcome": "rejected"})
                rejected.append(
                    {"source_id": source_id, "reason": "no_polygon_geometry", "geometry": original}
                )
                continue
            attrs = {c: (None if pd.isna(row[c]) else str(row[c])) for c in frame.columns if c != "geometry"}
            for component, polygon in enumerate(polygons):
                if role == "dedicated" and primary_includes_dedication:
                    accounting.append({"source_id": source_id, "component": component,
                                       "outcome": "superseded_by_current_access_layer"})
                    continue
                clipped = clipper.clip(polygon)
                clipped_parts = parts(clipped, {"Polygon"})
                accounting.append(
                    {
                        "source_id": source_id,
                        "component": component,
                        "outcome": "processed" if clipped_parts else "outside_england",
                    }
                )
                for i, part in enumerate(clipped_parts):
                    label = next(
                        (str(row[c]) for c in ["name", "Name", "NAME"] if c in row and pd.notna(row[c])), ""
                    )
                    records.append(
                        {
                            "component_id": f"{source_id}:{component}:{i}",
                            "source_id": source_id,
                            "name": label,
                            "source_attributes": json.dumps(attrs, sort_keys=True),
                            "geometry": part,
                        }
                    )
            if number and number % 2000 == 0:
                LOG.info("Normalising %s: %d/%d records", role, number, len(frame))
        LOG.info("Normalised %s: %d original records", role, len(frame))
    land = (
        gpd.GeoDataFrame(records, geometry="geometry", crs=CRS)
        if records
        else empty_frame(["component_id", "source_id", "name", "source_attributes"])
    )
    land = land.sort_values("component_id").reset_index(drop=True)
    # Preserve all provenance. Identical/overlapping components are reconciled when forming sites.
    reject_frame = (
        gpd.GeoDataFrame(rejected, geometry="geometry", crs=CRS)
        if rejected
        else empty_frame(["source_id", "reason"])
    )
    return land, accounting, reject_frame


def normalise_boundary(spec, role):
    frame = read_vector(spec, role)
    code = spec.get("code_field") or next((c for c in frame if c.upper().endswith("CD")), None)
    name = spec.get("name_field") or next((c for c in frame if c.upper().endswith("NM")), None)
    if not code or not name:
        raise ValueError(f"Set code_field and name_field for {role}; fields are {list(frame)}")
    result = frame[[code, name, "geometry"]].rename(columns={code: "code", name: "name"})
    result["geometry"] = shapely.make_valid(result.geometry.values)
    if result.geometry.is_empty.any() or result.geometry.isna().any() or result["code"].duplicated().any():
        raise ValueError(f"Invalid or duplicated {role} boundary records")
    return result


class RouteWriter:
    def __init__(self, path):
        self.path = path
        self.rows = []
        self.count = 0
        self.started = False

    def add(self, values):
        values["route_index"] = self.count
        self.count += 1
        self.rows.append(values)
        if len(self.rows) >= 10000:
            self.flush()

    def flush(self):
        if not self.rows:
            return
        frame = gpd.GeoDataFrame(self.rows, geometry="geometry", crs=CRS)[ROUTE_COLUMNS]
        frame.to_file(self.path, layer="routes", driver="GPKG", engine="pyogrio", append=self.started)
        self.started = True
        self.rows.clear()
        LOG.info("Prepared %d route segments", self.count)


def import_osm(pbf: Path, writer, barrier_path: Path, progress=None):
    import osmium
    from pyproj import Transformer

    transform = Transformer.from_crs(4326, CRS, always_xy=True).transform
    counts = collections.Counter()
    barrier_tags = {}
    total_ways, processed_ways = 0, 0

    class Count(osmium.SimpleHandler):
        def node(self, n):
            if "barrier" in n.tags:
                barrier_tags[n.id] = dict(n.tags)

        def way(self, w):
            nonlocal total_ways
            if "highway" in w.tags:
                counts.update(n.ref for n in w.nodes)
                if len(w.nodes) >= 2:
                    total_ways += 1

    LOG.info("OSM pass 1: identifying junctions and barriers")
    Count().apply_file(str(pbf), filters=[osmium.filter.KeyFilter("highway", "barrier")])
    junctions = {node for node, count in counts.items() if count > 1}
    counts.clear()
    if progress:
        progress("osm_ways", 0, total_ways)
    barrier_records = []

    class Import(osmium.SimpleHandler):
        def node(self, n):
            if n.id in barrier_tags:
                from shapely.geometry import Point

                tags = barrier_tags[n.id]
                barrier_records.append(
                    {
                        "node_id": str(n.id),
                        "blocking": barrier_blocks(tags),
                        "tags": json.dumps(tags),
                        "geometry": Point(transform(n.location.lon, n.location.lat)),
                    }
                )

        def way(self, w):
            nonlocal processed_ways
            if "barrier" in w.tags and len(w.nodes) >= 2:
                if any(not n.location.valid() for n in w.nodes):
                    raise ValueError(f"Barrier way {w.id} has missing node locations")
                coords = [transform(n.lon, n.lat) for n in w.nodes]
                if len(set(coords)) >= 2:
                    tags = dict(w.tags)
                    barrier_records.append(
                        {
                            "node_id": "",
                            "blocking": barrier_blocks(tags),
                            "tags": json.dumps(tags),
                            "geometry": LineString(coords),
                        }
                    )
            if "highway" not in w.tags or len(w.nodes) < 2:
                return
            tags = dict(w.tags)
            tier, seed, reason = pedestrian(tags)
            # Retain excluded lines as evidence but never connect them to the walking graph.
            segment, start = [], None
            for pos, n in enumerate(w.nodes):
                if not n.location.valid():
                    raise ValueError(f"OSM way {w.id} has missing node locations; extract is incomplete")
                xy = transform(n.lon, n.lat)
                if start is None:
                    start = n.ref
                segment.append(xy)
                if pos and (n.ref in junctions or n.ref in barrier_tags or pos == len(w.nodes) - 1):
                    if len(set(segment)) >= 2:
                        writer.add(
                            {
                                "source_id": f"osm:way:{w.id}:{pos}",
                                "tier": tier,
                                "seed": seed,
                                "start_node": f"osm:{start}",
                                "end_node": f"osm:{n.ref}",
                                "grade": grade(tags),
                                "reason": reason,
                                "name": tags.get("name", ""),
                                "geometry": LineString(segment),
                            }
                        )
                    segment, start = [xy], n.ref
            processed_ways += 1
            if progress and (processed_ways % 2000 == 0 or processed_ways == total_ways):
                progress("osm_ways", processed_ways, total_ways)

    LOG.info("OSM pass 2: streaming tagged route geometries")
    Import().apply_file(str(pbf), locations=True, idx="flex_mem", filters=[osmium.filter.KeyFilter("highway", "barrier")])
    writer.flush()
    barriers = (
        gpd.GeoDataFrame(barrier_records, geometry="geometry", crs=CRS)
        if barrier_records
        else empty_frame(["node_id", "blocking", "tags"])
    )
    barrier_tmp = barrier_path.with_suffix(".tmp.gpkg")
    barrier_tmp.unlink(missing_ok=True)
    write_layer(barriers, barrier_tmp, "barriers")
    os.replace(barrier_tmp, barrier_path)


def import_official(spec, writer, role="prow", replacement=None):
    path, layer = vector_source(spec, role)
    info = pyogrio.read_info(path, layer=layer)
    if not info["crs"] and not spec.get("crs"):
        raise ValueError("Rights-of-way source has no CRS")
    total, accepted = info["features"], 0
    id_field = spec.get("id_field")
    for offset in range(0, total, 20000):
        frame = pyogrio.read_dataframe(
            path, layer=layer, skip_features=offset, max_features=20000, use_arrow=False
        )
        if frame.crs is None:
            frame = frame.set_crs(spec["crs"])
        frame = frame.to_crs(CRS)
        for number, (_, row) in enumerate(frame.iterrows()):
            geom = row.geometry
            if geom is not None:
                geom = shapely.force_2d(geom)
            if replacement is not None:
                geom = geom.difference(replacement)
            lines = parts(shapely.make_valid(geom) if geom is not None else None, {"LineString"})
            if geom is not None and not geom.is_empty and not lines:
                raise ValueError(f"Unexpected official route geometry: {geom.geom_type}")
            for part_no, line in enumerate(lines):
                if line.length == 0:
                    continue
                sid = str(row[id_field]) if id_field else str(offset + number)
                writer.add(
                    {
                        "source_id": f"{role}:{sid}:{part_no}",
                        "tier": "official",
                        "seed": False,
                        "start_node": "",
                        "end_node": "",
                        "grade": "0:no:no",
                        "reason": "",
                        "name": "",
                        "geometry": line,
                    }
                )
                accepted += 1
    if not accepted:
        raise ValueError(f"No official route lines in {path}/{layer}")


def preparation_signature(config, sources, coverage_info):
    return fingerprint({"sources": sources, "config": config,
                        "overrides": {spec["authority"]: source_digest(Path(spec["path"])) for spec in config.get("overrides", [])},
                        "coverage": sorted(coverage_info.get("missing_authorities", []), key=lambda row: row["code"]),
                        "code": code_signature("prepare.py", "common.py", "access.py")})


def import_signature(source):
    # Export changes and coverage updates do not require reparsing a multi-GB PBF.
    return fingerprint({"source_sha256": source["sha256"], "importer": inspect.getsource(import_osm),
                        "writer": inspect.getsource(RouteWriter), "columns": ROUTE_COLUMNS, "crs": CRS,
                        "access_policy": digest(Path(__file__).with_name("access.py"))})


def prepare(config, root: Path, refresh=False):
    sources = read_json(root / "sources.json")
    if not sources:
        raise ValueError("Run download first")
    for role, spec in sources.items():
        if spec.get("local"):
            spec["sha256"] = source_digest(Path(spec["path"]))
    signature = preparation_signature(config, sources, read_json(root / "coverage.json", {}))
    prepared = root / "prepared"
    prepared.mkdir(parents=True, exist_ok=True)
    old = read_json(prepared / "manifest.json", {})
    if (
        not refresh
        and old.get("signature") == signature
        and all((prepared / p).exists() for p in old.get("files", []))
    ):
        LOG.info("Reusing prepared dataset")
        return old
    # Never expose a previous manifest while replacing its underlying dataset.
    (prepared / "manifest.json").unlink(missing_ok=True)
    tmp = prepared / "land.tmp.gpkg"
    tmp.unlink(missing_ok=True)
    boundaries = {
        role: normalise_boundary(sources[role], role) for role in ["countries", "counties", "councils"]
    }
    england = boundaries["countries"].loc[boundaries["countries"].code == "E92000001"]
    if england.empty:
        raise ValueError("Country boundaries do not contain England (E92000001)")
    country = england.geometry.union_all()
    land, accounting, rejected = normalise_land(sources, country)
    for role, frame in boundaries.items():
        write_layer(frame, tmp, role)
    write_layer(land, tmp, "land")
    write_layer(rejected, tmp, "rejected")
    missing = {x["code"] for x in read_json(root / "coverage.json", {}).get("missing_authorities", [])}
    gaps = pd.concat([f.loc[f.code.isin(missing)] for f in boundaries.values()], ignore_index=True)
    gap_frame = gpd.GeoDataFrame(gaps, geometry="geometry", crs=CRS)
    matched = set(gap_frame.code)
    # Unmatched missing authorities must not quietly disappear from coverage diagnostics.
    unmapped = sorted(missing - matched)
    write_layer(gap_frame, tmp, "coverage_gaps")
    os.replace(tmp, prepared / "land.gpkg")
    atomic_json(prepared / "accounting.json", accounting)
    routes_tmp = prepared / "routes.tmp.gpkg"
    routes_tmp.unlink(missing_ok=True)
    osm_signature = import_signature(sources["osm"])
    cached_osm = prepared / "osm.gpkg"
    osm_meta = read_json(prepared / "osm.json", {})
    if (
        refresh
        or osm_meta.get("signature") != osm_signature
        or not cached_osm.exists()
        or not (prepared / "barriers.gpkg").exists()
    ):
        osm_tmp = prepared / "osm.tmp.gpkg"
        osm_tmp.unlink(missing_ok=True)
        osm_writer = RouteWriter(osm_tmp)
        import_osm(Path(sources["osm"]["path"]), osm_writer, prepared / "barriers.gpkg")
        if not osm_writer.started:
            raise ValueError("OSM extract has no highway geometries")
        os.replace(osm_tmp, cached_osm)
        osm_meta = {"signature": osm_signature, "route_count": osm_writer.count}
        atomic_json(prepared / "osm.json", osm_meta)
    else:
        LOG.info("Reusing completed OSM import")
    shutil.copyfile(cached_osm, routes_tmp)
    writer = RouteWriter(routes_tmp)
    writer.count = osm_meta["route_count"]
    writer.started = True
    replacement_geometries = []
    for override in config.get("overrides", []):
        boundary = boundaries["councils"].loc[boundaries["councils"].code == override["authority"]]
        if boundary.empty:
            raise ValueError(f"Unknown replacement authority: {override['authority']}")
        replacement_geometries.extend(boundary.geometry)
    replacement = shapely.union_all(replacement_geometries) if replacement_geometries else None
    import_official(sources["prow"], writer, replacement=replacement)
    for override in config.get("overrides", []):
        # A council replacement must remain inside the explicitly declared authority.
        boundary = (
            boundaries["councils"]
            .loc[boundaries["councils"].code == override["authority"]]
            .geometry.union_all()
        )
        frame = read_vector(override, "override")
        if not frame.geometry.covered_by(boundary.buffer(5)).all():
            raise ValueError("Council replacement extends outside its declared authority")
        import_official(override, writer, role=f"council:{override['authority']}")
    writer.flush()
    with sqlite3.connect(routes_tmp) as db:
        db.execute("CREATE UNIQUE INDEX IF NOT EXISTS routes_lookup ON routes(route_index)")
    db.close()
    os.replace(routes_tmp, prepared / "routes.gpkg")
    value = {
        "signature": signature,
        "route_count": writer.count,
        "land_components": len(land),
        "unmapped_missing_authorities": unmapped,
        "files": ["land.gpkg", "routes.gpkg", "barriers.gpkg", "accounting.json"],
        "sources": {**sources, **{f"council:{spec['authority']}": {**spec, "local": True,
                   "sha256": source_digest(Path(spec["path"]))} for spec in config.get("overrides", [])}},
        "synthetic": bool(config.get("synthetic")),
        "land_sha256": digest(prepared / "land.gpkg"),
    }
    atomic_json(prepared / "manifest.json", value)
    return value
