"""Global connectivity with tiled geometry joins and compact component arrays."""

from __future__ import annotations

import json
import logging
import math
import os
import sqlite3
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import pyogrio
import shapely
from shapely.geometry import Point, box

from .common import (
    CRS,
    UnionFind,
    atomic_json,
    code_signature,
    empty_frame,
    fingerprint,
    read_json,
    write_layer,
)

LOG = logging.getLogger(__name__)
SAFE = {"official", "public", "inferred"}
PERMITTED = SAFE | {"permissive"}
POSSIBLE = SAFE | {"permissive", "unknown"}


def form_sites(land: gpd.GeoDataFrame):
    uf = UnionFind(len(land))
    tree = land.sindex
    for i, geom in enumerate(land.geometry):
        for j in tree.query(geom, predicate="intersects"):
            if j <= i:
                continue
            overlap = geom.intersection(land.geometry.iloc[j])
            if overlap.area > 0 or overlap.length > 0:
                uf.union(i, int(j))
    groups = {}
    for i in range(len(land)):
        groups.setdefault(uf.find(i), []).append(i)
    records, membership = [], []
    for indices in groups.values():
        subset = land.iloc[indices]
        members = sorted(subset.component_id.tolist())
        sid = "site-" + fingerprint(members)[:16]
        geometry = subset.geometry.union_all()
        names = sorted({str(x) for x in subset["name"] if str(x)})
        records.append(
            {
                "site_id": sid,
                "name": "; ".join(names) or f"Unnamed access land {sid[5:13]}",
                "source_ids": ";".join(sorted(set(subset.source_id))),
                "parcel_count": len(set(subset.source_id)),
                "component_count": len(indices),
                "area_ha": geometry.area / 10000,
                "geometry": geometry,
            }
        )
        membership.extend({"site_id": sid, "component_id": m} for m in members)
    sites = (
        gpd.GeoDataFrame(records, geometry="geometry", crs=CRS)
        if records
        else empty_frame(["site_id", "name", "source_ids", "parcel_count", "component_count", "area_ha"])
    )
    return sites.sort_values("site_id").reset_index(drop=True), membership


def boundary_labels(sites, boundary, prefix):
    names, codes, all_names, all_codes = [], [], [], []
    tree = boundary.sindex
    for geom in sites.geometry:
        intersections = []
        for i in tree.query(geom, predicate="intersects"):
            area = geom.intersection(boundary.geometry.iloc[i]).area
            if area > 0:
                intersections.append((area, str(boundary.iloc[i]["code"]), str(boundary.iloc[i]["name"])))
        intersections.sort(key=lambda x: (-x[0], x[1]))
        codes.append(intersections[0][1] if intersections else "")
        names.append(intersections[0][2] if intersections else "")
        all_codes.append(";".join(x[1] for x in intersections))
        all_names.append(";".join(x[2] for x in intersections))
    sites[f"{prefix}_code"] = codes
    sites[prefix] = names
    sites[f"all_{prefix}_codes"] = all_codes
    sites["all_counties" if prefix == "county" else f"all_{prefix}s"] = all_names


def create_spool(path, signature):
    con = sqlite3.connect(path)
    con.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS edges (
            a INTEGER, b INTEGER, distance REAL, weak INTEGER,
            PRIMARY KEY(a,b,distance,weak));
        CREATE TABLE IF NOT EXISTS seeds (route INTEGER PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS tiles (tile TEXT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS notes (site INTEGER, reason TEXT, PRIMARY KEY(site,reason));
        CREATE TABLE IF NOT EXISTS contacts (
            site INTEGER, route INTEGER, distance REAL, weak INTEGER,
            PRIMARY KEY(site,route));
    """)
    old = con.execute("SELECT value FROM metadata WHERE key='signature'").fetchone()
    if old and old[0] != signature:
        con.executescript(
            "DELETE FROM edges; DELETE FROM seeds; DELETE FROM tiles; DELETE FROM notes; DELETE FROM contacts;"
        )
        con.execute("DELETE FROM metadata WHERE key='nodes_done'")
    con.execute("INSERT OR REPLACE INTO metadata VALUES ('signature',?)", (signature,))
    con.commit()
    return con


def network_nodes(con, routes_path, barriers):
    if con.execute("SELECT value FROM metadata WHERE key='nodes_done'").fetchone():
        LOG.info("Reusing OSM node connections")
        return
    LOG.info("Connecting routes through shared OSM nodes")
    blocking_nodes = set(barriers.loc[barriers.blocking.astype(bool), "node_id"])
    db = sqlite3.connect(routes_path)
    query = """SELECT node, route_index FROM (
        SELECT start_node AS node, route_index FROM routes WHERE start_node != '' AND tier != 'excluded'
        UNION ALL SELECT end_node AS node, route_index FROM routes WHERE end_node != '' AND tier != 'excluded'
        ) ORDER BY node, route_index"""
    first, last, batch = None, None, []
    for node, index in db.execute(query):
        if node.startswith("osm:") and node[4:] in blocking_nodes:
            continue
        if node == last:
            batch.append((first, index, 0.0, 0))
        else:
            first, last = index, node
        if len(batch) >= 50000:
            con.executemany("INSERT OR IGNORE INTO edges VALUES (?,?,?,?)", batch)
            batch.clear()
    con.executemany("INSERT OR IGNORE INTO edges VALUES (?,?,?,?)", batch)
    con.execute("INSERT OR REPLACE INTO metadata VALUES ('nodes_done','true')")
    con.commit()
    db.close()
    LOG.info("OSM node connections complete")


def tile_joins(
    con, sites, routes_path, barriers, count, tile_size, max_distance, progress=None, official_site_count=None
):
    info = pyogrio.read_info(routes_path, layer="routes")
    bounds = info["total_bounds"]
    if not bounds:
        return
    site_tree = sites.sindex
    barrier_tree = barriers.sindex
    x0, y0, x1, y1 = bounds
    done = {r[0] for r in con.execute("SELECT tile FROM tiles")}
    total_tiles = (math.floor(x1 / tile_size) - math.floor(x0 / tile_size) + 1) * (
        math.floor(y1 / tile_size) - math.floor(y0 / tile_size) + 1
    )
    if progress:
        progress("tiles", len(done), total_tiles)
    LOG.info("Joining route geometry in tiles (%d already complete)", len(done))
    processed = 0
    for x in range(math.floor(x0 / tile_size), math.floor(x1 / tile_size) + 1):
        for y in range(math.floor(y0 / tile_size), math.floor(y1 / tile_size) + 1):
            key = f"{x}:{y}"
            if key in done:
                continue
            extent = box(x * tile_size, y * tile_size, (x + 1) * tile_size, (y + 1) * tile_size)
            routes = pyogrio.read_dataframe(
                routes_path, layer="routes", bbox=extent.buffer(max_distance).bounds, use_arrow=True
            )
            if routes.empty:
                con.execute("INSERT INTO tiles VALUES (?)", (key,))
                con.commit()
                processed += 1
                if progress:
                    progress("tiles", len(done) + processed, total_tiles)
                continue
            routes = routes.reset_index(drop=True)
            route_tree = routes.sindex
            local_routes = routes.geometry.intersection(extent)
            grade_buffers = routes.geometry.loc[routes.grade != "0:no:no"].buffer(max_distance)
            grade_tree = grade_buffers.sindex
            edges, contacts, notes, seeds = [], [], [], []
            # Official geometry has no OSM node IDs. Match its endpoints to compatible nearby lines.
            for row in routes.loc[routes.tier == "official"].itertuples():
                local = row.Index
                for xy in [row.geometry.coords[0], row.geometry.coords[-1]]:
                    point = Point(xy)
                    if not extent.covers(point):
                        continue
                    for other in route_tree.query(point.buffer(max_distance)):
                        other_row = routes.iloc[other]
                        if other == local or other_row.tier == "excluded" or other_row.grade != row.grade:
                            continue
                        distance = point.distance(other_row.geometry)
                        if distance > max_distance:
                            continue
                        connection = shapely.shortest_line(point, other_row.geometry)
                        blocked = any(
                            bool(barriers.iloc[k].blocking)
                            and barriers.geometry.iloc[k].distance(connection) < 0.25
                            for k in barrier_tree.query(connection.buffer(0.25))
                        )
                        a, b = sorted([int(row.route_index), int(other_row.route_index)])
                        edges.append((a, b, distance, int(blocked)))
            for site_index in site_tree.query(extent.buffer(max_distance)):
                geom = sites.geometry.iloc[site_index]
                for local in route_tree.query(geom.buffer(max_distance)):
                    row = routes.iloc[local]
                    route = local_routes.iloc[local]
                    if route.is_empty:
                        continue
                    distance = geom.distance(route)
                    if distance > max_distance:
                        continue
                    contact_geometry = (
                        geom.intersection(route) if distance == 0 else shapely.shortest_line(geom, route)
                    )
                    blocked = any(
                        bool(barriers.iloc[k].blocking)
                        and barriers.geometry.iloc[k].distance(contact_geometry) < 0.25
                        for k in barrier_tree.query(contact_geometry.buffer(0.25))
                    )
                    # Shared boundary without an entrance is weak evidence, not guaranteed access.
                    boundary_only = (
                        geom.boundary.intersection(route).length > 0
                        and geom.intersection(route).length <= geom.boundary.intersection(route).length
                    )
                    boundary_only = boundary_only or (
                        bool(row.seed) and distance == 0 and geom.intersection(route).length == 0
                    )
                    entrance = any(
                        not bool(barriers.iloc[k].blocking)
                        and barriers.geometry.iloc[k].distance(contact_geometry) < max(max_distance, 0.25)
                        for k in barrier_tree.query(contact_geometry.buffer(max(max_distance, 0.25)))
                    )
                    grade_separated = row.grade != "0:no:no"
                    if not grade_separated and row.tier == "official":
                        elevated = grade_tree.query(contact_geometry, predicate="intersects")
                        if len(elevated):
                            cover = shapely.union_all(grade_buffers.iloc[elevated].values)
                            grade_separated = contact_geometry.difference(cover).is_empty
                    weak = blocked or (boundary_only and not entrance) or grade_separated
                    idx = int(row.route_index)
                    contacts.append((int(site_index), idx, distance, int(weak)))
                    if row.tier != "excluded":
                        edges.append((idx, count + int(site_index), distance, int(weak)))
                    if blocked:
                        notes.append((int(site_index), "mapped_barrier_at_contact"))
                    if boundary_only and not entrance:
                        notes.append((int(site_index), "boundary_contact_without_entrance"))
                    if grade_separated and row.tier != "excluded":
                        notes.append((int(site_index), "possible_grade_separation_at_contact"))
            for row in routes.loc[routes.seed.astype(bool)].itertuples():
                candidates = site_tree.query(row.geometry, predicate="intersects")
                if official_site_count is not None:
                    candidates = candidates[candidates < official_site_count]
                covered = (
                    shapely.union_all(sites.geometry.iloc[candidates].values) if len(candidates) else None
                )
                outside = row.geometry if covered is None else row.geometry.difference(covered)
                if outside.length > 5:
                    seeds.append((int(row.route_index),))
            con.executemany("INSERT OR IGNORE INTO edges VALUES (?,?,?,?)", edges)
            # Keep the strongest contact across tile seams, with its matching distance.
            con.executemany(
                """INSERT INTO contacts VALUES (?,?,?,?)
                ON CONFLICT(site,route) DO UPDATE SET
                distance = CASE
                    WHEN excluded.weak < contacts.weak THEN excluded.distance
                    WHEN excluded.weak = contacts.weak THEN min(contacts.distance, excluded.distance)
                    ELSE contacts.distance END,
                weak = min(contacts.weak, excluded.weak)""",
                contacts,
            )
            con.executemany("INSERT OR IGNORE INTO notes VALUES (?,?)", notes)
            con.executemany("INSERT OR IGNORE INTO seeds VALUES (?)", seeds)
            con.execute("INSERT INTO tiles VALUES (?)", (key,))
            con.commit()
            processed += 1
            if progress:
                progress("tiles", len(done) + processed, total_tiles)
            if processed % 10 == 0:
                LOG.info("Spatial joins: %d additional tiles complete", processed)
    # Nearby sites are possible walking connections only: corner contacts and mapping gaps remain uncertain.
    for i, geom in enumerate(sites.geometry):
        for j in site_tree.query(geom.buffer(max_distance)):
            if j <= i:
                continue
            distance = geom.distance(sites.geometry.iloc[j])
            if distance <= max_distance:
                con.execute(
                    "INSERT OR IGNORE INTO edges VALUES (?,?,?,?)", (count + i, count + int(j), distance, 1)
                )
                con.execute("INSERT OR IGNORE INTO notes VALUES (?,?)", (i, "uncertain_land_connection"))
                con.execute("INSERT OR IGNORE INTO notes VALUES (?,?)", (int(j), "uncertain_land_connection"))
    con.commit()


def regional_boundary_notes(con, sites, tiers, manifest, tolerance):
    """Unresolved possible components reaching a cut edge remain uncertain."""
    count = len(tiers)
    uf = UnionFind(count + len(sites))
    allowed = np.array([t in POSSIBLE for t in tiers] + [True] * len(sites))
    for a, b in con.execute("SELECT a,b FROM edges WHERE distance <= ?", (max(5, tolerance),)):
        if allowed[a] and allowed[b]:
            uf.union(a, b)
    roots = {uf.find(int(r)) for r in manifest.get("boundary_routes", []) if allowed[int(r)]}
    site_ids = set(manifest.get("boundary_sites", []))
    roots.update(uf.find(count + i) for i, sid in enumerate(sites.site_id) if sid in site_ids)
    notes = [(i, "regional_context_incomplete") for i in range(len(sites)) if uf.find(count + i) in roots]
    con.executemany("INSERT OR IGNORE INTO notes VALUES (?,?)", notes)
    con.commit()


def reachability(con, tiers, site_count, tolerance, possible=False, permissive=False, hint_count=0):
    count = len(tiers)
    allowed = np.array(
        [t in (POSSIBLE if possible else PERMITTED if permissive else SAFE) for t in tiers]
        + [True] * site_count
        + [possible] * hint_count
    )
    uf = UnionFind(count + site_count + hint_count)
    for a, b, distance, weak in con.execute(
        "SELECT a,b,distance,weak FROM edges WHERE distance <= ?", (tolerance,)
    ):
        if allowed[a] and allowed[b] and (possible or not weak):
            uf.union(a, b)
    seed_roots = {uf.find(i) for (i,) in con.execute("SELECT route FROM seeds") if allowed[i]}
    return [uf.find(count + i) in seed_roots for i in range(site_count)]


def classify(con, sites, tiers, tolerance, gaps, unmapped=False, min_candidate_area_m2=100, hint_count=0):
    LOG.info("Tracing connectivity at exact, default and relaxed tolerances")
    baseline = reachability(con, tiers, len(sites), tolerance, hint_count=hint_count)
    exact = reachability(con, tiers, len(sites), 0, hint_count=hint_count)
    relaxed = reachability(con, tiers, len(sites), max(5, tolerance), hint_count=hint_count)
    permitted = [
        reachability(con, tiers, len(sites), t, permissive=True, hint_count=hint_count)
        for t in [0, tolerance, max(5, tolerance)]
    ]
    possible = reachability(con, tiers, len(sites), max(5, tolerance), possible=True, hint_count=hint_count)
    reasons = {}
    for i, reason in con.execute("SELECT site,reason FROM notes"):
        reasons.setdefault(i, set()).add(reason)
    gap_tree = gaps.sindex
    statuses, strengths, labels = [], [], []
    for i, geom in enumerate(sites.geometry):
        notes = reasons.get(i, set()).copy()
        if geom.area < min_candidate_area_m2:
            # Keep the full inventory, but do not promote tiny boundary fragments
            # to candidates solely because no mapped approach touches them.
            notes.add("very_small_site_geometry")
        missing = unmapped or len(gap_tree.query(geom, predicate="intersects")) > 0
        if missing:
            notes.add("official_coverage_missing")
        contacts = list(con.execute("SELECT route,distance,weak FROM contacts WHERE site=?", (i,)))
        if any(tiers[r] == "unknown" for r, _, _ in contacts):
            notes.add("nearby_route_access_unknown")
        if any(tiers[r] == "permissive" for r, _, _ in contacts):
            notes.add("permissive_route_nearby")
        if exact[i] != relaxed[i]:
            notes.add("tolerance_sensitive")
        if baseline[i] and exact[i] == relaxed[i]:
            status = "access_evidenced"
            notes.add("connected_to_pedestrian_road")
            strength = "mapped_connection"
        elif all(values[i] for values in permitted):
            status, strength = "permissive_access_evidenced", "mapped_permissive_connection"
            notes.add("connected_via_permissive_route")
            notes.add("permissive_access_may_be_withdrawn")
        elif baseline[i] or possible[i] or missing or notes:
            status, strength = "uncertain", "review_required"
            if possible[i] and not baseline[i]:
                notes.add("only_weak_or_permissive_connection")
        else:
            status, strength = "likely_access_island", "screening_candidate"
            notes.add("no_evidenced_network_connection")
            if any(tiers[r] in SAFE for r, _, _ in contacts):
                notes.add("public_route_disconnected_from_roads")
        statuses.append(status)
        strengths.append(strength)
        labels.append(";".join(sorted(notes)))
    sites["status"], sites["evidence_strength"], sites["reasons"] = statuses, strengths, labels
    sites["exact_access"] = exact
    sites["default_access"] = baseline
    sites["relaxed_access"] = relaxed
    for label, values in zip(["exact", "default", "relaxed"], permitted):
        sites[f"{label}_permissive_access"] = values
    LOG.info("Classified sites: %s", sites.status.value_counts().to_dict())
    return sites


def add_nearest_distance(sites, routes_path, limit=10000):
    # Read each neighbourhood once rather than reopening the national GeoPackage
    # for every site. Keep the original doubling-search ceiling (6.4 km by default).
    if limit < 50:
        sites["nearest_mapped_route_m"] = np.nan
        return
    radius = 50
    while radius * 2 <= limit:
        radius *= 2
    distances = np.full(len(sites), np.nan)
    points = sites.geometry.representative_point()
    groups = {}
    for i, point in enumerate(points):
        groups.setdefault((math.floor(point.x / 20000), math.floor(point.y / 20000)), []).append(i)
    for indices in groups.values():
        geometry = sites.geometry.iloc[indices]
        west, south, east, north = geometry.total_bounds
        frame = pyogrio.read_dataframe(
            routes_path,
            layer="routes",
            bbox=(west - radius, south - radius, east + radius, north + radius),
            columns=[],
            use_arrow=True,
        )
        if frame.empty:
            continue
        pairs, values = frame.sindex.nearest(
            geometry.values, return_all=False, max_distance=radius, return_distance=True
        )
        distances[np.asarray(indices)[pairs[0]]] = values
    sites["nearest_mapped_route_m"] = distances


def analyse(config, root: Path, output: Path, authority=None, bbox=None, refresh=False, progress=None):
    prepared = root / "prepared"
    manifest = read_json(prepared / "manifest.json")
    if not manifest:
        raise ValueError("Run prepare first")
    settings = config.get("analysis", {})
    tolerance = float(settings.get("tolerance_m", 2))
    tile_size = int(settings.get("tile_size_m", 20000))
    min_candidate_area_m2 = float(settings.get("min_candidate_area_m2", 100))
    if tolerance < 0 or tile_size < 100:
        raise ValueError("Tolerance must be nonnegative; tile size must be >=100 m")
    if not math.isfinite(min_candidate_area_m2) or min_candidate_area_m2 < 0:
        raise ValueError("Minimum candidate area must be finite and nonnegative")
    signature = fingerprint(
        {
            "prepared": manifest["signature"],
            "analysis": settings,
            "coverage": read_json(root / "coverage.json", {}).get("missing_authorities", []),
            "authority": authority,
            "bbox": bbox,
            "code": code_signature(
                "analyse.py", "common.py", "access.py", "access_land.py", "export.py", "reports.py"
            ),
        }
    )
    output.mkdir(parents=True, exist_ok=True)
    previous = read_json(output / "analysis.json", {})
    if not refresh and previous.get("signature") == signature and (output / "results.gpkg").exists():
        LOG.info("Reusing analysis results")
        return previous
    (output / "analysis.json").unlink(missing_ok=True)
    land_path, routes_path = prepared / "land.gpkg", prepared / "routes.gpkg"
    land = pyogrio.read_dataframe(land_path, layer="land", use_arrow=True)
    if manifest.get("catalogue"):
        sites = (
            pyogrio.read_dataframe(land_path, layer="sites", use_arrow=True)
            .sort_values("site_id")
            .reset_index(drop=True)
        )
        membership = pd.read_csv(prepared / "membership.csv").to_dict(orient="records")
    else:
        sites, membership = form_sites(land)
    from .access_land import annotate_sites

    if "access_regimes" not in sites:
        sites = annotate_sites(sites, land, membership)
    full_ids = {s: i for i, s in enumerate(sites.site_id)}
    LOG.info("Formed %d connected sites from %d land components", len(sites), len(land))
    counties = pyogrio.read_dataframe(land_path, layer="counties")
    councils = pyogrio.read_dataframe(land_path, layer="councils")
    gaps = pyogrio.read_dataframe(land_path, layer="coverage_gaps")
    gaps = gaps.drop_duplicates("code").reset_index(drop=True)
    replacement_codes = {x["authority"] for x in config.get("overrides", [])}
    gaps = gaps.loc[~gaps.code.isin(replacement_codes)].reset_index(drop=True)
    barriers = pyogrio.read_dataframe(prepared / "barriers.gpkg", layer="barriers")
    count = manifest["route_count"]
    db = sqlite3.connect(routes_path)
    tiers = [t[0] for t in db.execute("SELECT tier FROM routes ORDER BY route_index")]
    db.close()
    if len(tiers) != count:
        raise ValueError("Route count differs from preparation manifest")
    graph_dir = root / "analysis"
    graph_dir.mkdir(parents=True, exist_ok=True)
    graph_signature = fingerprint(
        {
            "prepared": manifest["signature"],
            "analysis": {key: value for key, value in settings.items() if key != "review_signature"},
            "code": code_signature("analyse.py", "common.py", "access.py"),
        }
    )
    con = create_spool(graph_dir / "connectivity.sqlite", graph_signature)
    hints = empty_frame(["site_id", "source_id", "name", "access", "foot"])
    if "unverified_commons" in {row[0] for row in pyogrio.list_layers(land_path)}:
        hints = pyogrio.read_dataframe(land_path, layer="unverified_commons")
    # Private/no common tags are display clues, never walking graph nodes.
    from .access import pedestrian

    walkable_hints = hints.loc[
        [pedestrian({"foot": row.foot, "access": row.access})[0] != "excluded" for row in hints.itertuples()]
    ]
    graph_sites = gpd.GeoDataFrame(pd.concat([sites, walkable_hints], ignore_index=True), crs=CRS)
    nearby = []
    for geometry in sites.geometry:
        nearby.append(
            ";".join(hints.source_id.iloc[hints.sindex.query(geometry.buffer(max(5, tolerance)))].tolist())
        )
    sites["unverified_common_ids"] = nearby
    try:
        if progress:
            progress("network_nodes", 0, count)
        network_nodes(con, routes_path, barriers)
        if progress:
            progress("network_nodes", count, count)
        tile_joins(
            con, graph_sites, routes_path, barriers, count, tile_size, max(5, tolerance), progress, len(sites)
        )
        for i, ids in enumerate(nearby):
            if ids:
                con.execute("INSERT OR IGNORE INTO notes VALUES (?,?)", (i, "unverified_osm_common_nearby"))
        if manifest.get("region"):
            context = pyogrio.read_dataframe(land_path, layer="context").geometry.union_all()
            hint_edges = walkable_hints.loc[~walkable_hints.geometry.covered_by(context), "site_id"].tolist()
            regional_boundary_notes(
                con,
                graph_sites,
                tiers,
                {**manifest, "boundary_sites": manifest["boundary_sites"] + hint_edges},
                tolerance,
            )
        if progress:
            progress("classify_sites", 0, len(sites))
        sites = classify(
            con,
            sites,
            tiers,
            tolerance,
            gaps,
            bool(manifest["unmapped_missing_authorities"]),
            min_candidate_area_m2,
            len(walkable_hints),
        )
        if progress:
            progress("classify_sites", len(sites), len(sites))
        evidence = pd.read_sql_query("SELECT site,route,distance,weak FROM contacts", con)
    finally:
        con.close()
    if "county" not in sites:
        boundary_labels(sites, counties, "county")
        boundary_labels(sites, councils, "council")
    LOG.info("County and council labels complete")
    # Some metropolitan and London councils do not occur in the county/unitary layer.
    fallback = sites.county == ""
    sites.loc[fallback, "county"] = sites.loc[fallback, "council"]
    sites.loc[fallback, "county_code"] = sites.loc[fallback, "council_code"]
    if authority:
        if authority not in set(councils.code) | set(counties.code):
            raise ValueError(f"Unknown authority code: {authority}")
        mask = sites.all_council_codes.str.split(";").apply(
            lambda xs: authority in xs
        ) | sites.all_county_codes.str.split(";").apply(lambda xs: authority in xs)
        sites = sites.loc[mask].copy()
    if bbox:
        sites = sites.loc[sites.intersects(box(*bbox))].copy()
    if manifest.get("region"):
        sites = sites.loc[sites.site_id.isin(manifest["region"]["site_ids"])].copy()
    add_nearest_distance(sites, routes_path)
    LOG.info("Nearest-route distances complete for %d reported sites", len(sites))
    points = sites.geometry.representative_point()
    lonlat = points.to_crs(4326)
    sites["easting"], sites["northing"] = points.x, points.y
    sites["longitude"], sites["latitude"] = lonlat.x, lonlat.y
    sites["source_versions"] = json.dumps(
        {
            role: spec.get("version", spec.get("last_modified", spec.get("sha256")))
            for role, spec in manifest["sources"].items()
        },
        sort_keys=True,
    )
    sites["country"] = "England"
    sites["geometry_sha256"] = pd.Series(
        [fingerprint(shapely.normalize(geometry).wkb_hex) for geometry in sites.geometry],
        index=sites.index,
        dtype="object",
    )
    tmp = output / "results.tmp.gpkg"
    tmp.unlink(missing_ok=True)
    write_layer(sites, tmp, "sites")
    write_layer(sites.loc[sites.status == "likely_access_island"], tmp, "likely_access_islands")
    write_layer(sites.loc[sites.status == "permissive_access_evidenced"], tmp, "permissive_access_evidenced")
    write_layer(hints, tmp, "unverified_commons")
    write_layer(gaps, tmp, "coverage_gaps")
    write_layer(barriers, tmp, "barriers")
    rejected = pyogrio.read_dataframe(land_path, layer="rejected")
    write_layer(rejected, tmp, "rejected_land")
    if manifest.get("region"):
        write_layer(pyogrio.read_dataframe(land_path, layer="study_area"), tmp, "study_area")
        write_layer(pyogrio.read_dataframe(land_path, layer="context"), tmp, "context")
    os.replace(tmp, output / "results.gpkg")
    if (prepared / "lineage.csv").exists():
        lineage = pd.read_csv(prepared / "lineage.csv")
        lineage.loc[lineage.site_id.isin(sites.site_id)].to_csv(output / "site_lineage.csv", index=False)
    selected = {full_ids[s]: s for s in sites.site_id}
    evidence = evidence.loc[evidence.site.isin(selected)]
    evidence["site_id"] = evidence.site.map(selected)
    if not evidence.empty:
        evidence["tier"] = evidence.route.map(lambda r: tiers[int(r)])
    else:
        evidence["tier"] = pd.Series(dtype="str")
    # Geometry evidence is restricted to routes near reported sites, avoiding another full national copy.
    route_indices = sorted(set(evidence.route))
    route_source_ids = {}
    for start in range(0, len(route_indices), 5000):
        ids = ",".join(str(int(i)) for i in route_indices[start : start + 5000])
        frame = pyogrio.read_dataframe(
            routes_path, layer="routes", where=f"route_index IN ({ids})", use_arrow=True
        )
        route_source_ids.update(zip(frame.route_index, frame.source_id))
        if start == 0:
            write_layer(frame, output / "results.gpkg", "route_evidence")
        else:
            frame.to_file(
                output / "results.gpkg", layer="route_evidence", driver="GPKG", engine="pyogrio", append=True
            )
    if not route_indices:
        write_layer(
            empty_frame(["route_index", "source_id", "tier", "reason"]),
            output / "results.gpkg",
            "route_evidence",
        )
    evidence["source_id"] = evidence.route.map(route_source_ids)
    evidence.drop(columns="site").to_csv(output / "route_contacts.csv", index=False)
    membership_table = pd.DataFrame(membership, columns=["site_id", "component_id"])
    membership_table.loc[membership_table.site_id.isin(sites.site_id)].to_csv(
        output / "site_membership.csv", index=False
    )
    accounting = read_json(prepared / "accounting.json", [])
    rejected_count = sum(r["outcome"] == "rejected" for r in accounting)
    value = {
        "signature": signature,
        "complete": rejected_count == 0,
        "scope": {
            "authority": authority,
            "bbox": bbox,
            "country": "England",
            "synthetic": bool(manifest.get("synthetic")),
            "region": manifest.get("region"),
        },
        "counts": {str(k): int(v) for k, v in sites.status.value_counts().items()},
        "sites": len(sites),
        "original_components": len(accounting),
        "rejected_components": rejected_count,
        "land_components": len(land),
        "route_segments": count,
        "registered_common_sites": int(sites.registered_common_land.sum()),
        "section15_sites": int(sites.access_regimes.str.contains("section15_statutory_access").sum()),
        "unverified_osm_commons": len(hints),
        "sources": manifest["sources"],
        "coverage": {
            **read_json(root / "coverage.json", {}),
            "missing_authorities": gaps[["code", "name"]].to_dict(orient="records"),
        },
        "unmapped_missing_authorities": manifest["unmapped_missing_authorities"],
        "parameters": {
            "tolerance_m": tolerance,
            "tile_size_m": tile_size,
            "min_candidate_area_m2": min_candidate_area_m2,
        },
        "interpretation": "Screening evidence only; absence of a mapped connection is not proof of no lawful access.",
    }
    atomic_json(output / "analysis.json", value)
    atomic_json(output / "component_accounting.json", accounting)
    return value
