"""Independent regional input caches built from shared reference data."""

from __future__ import annotations

import inspect
import logging
import os
import re
import time
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import pyogrio
import shapely
from filelock import FileLock
from shapely.geometry import Polygon, box

from .common import (
    CRS,
    atomic_json,
    digest,
    empty_frame,
    fingerprint,
    now,
    read_json,
    source_digest,
    write_layer,
)

LOG = logging.getLogger(__name__)
PRESETS = {
    "dorset": {"name": "Dorset", "authorities": ["E06000059"], "osm_slug": "dorset"},
    "isle-of-wight": {"name": "Isle of Wight", "authorities": ["Isle of Wight"], "osm_slug": "isle-of-wight"},
    "devon": {"name": "Devon", "authorities": ["Devon"], "osm_slug": "devon"},
    "cumbria": {
        "name": "Cumbria",
        "authorities": ["Cumberland", "Westmorland and Furness"],
        "osm_slug": "cumbria",
    },
}


def slugify(name):
    return re.sub(r"[^a-z0-9]+", "-", name.casefold()).strip("-") or "region"


class Progress:
    def __init__(self, path):
        self.path = path
        self.started = time.monotonic()
        self.state = {"started": now(), "complete": False}
        self("starting", 0, None)

    def __call__(self, stage, completed=0, total=None, **details):
        self.state.update(
            stage=stage,
            completed=int(completed),
            total=total,
            elapsed_seconds=round(time.monotonic() - self.started, 1),
            updated=now(),
            **details,
        )
        atomic_json(self.path, self.state)
        LOG.info("%s: %s / %s", stage, completed, total if total is not None else "pending")


def ensure_reference(config, root, refresh=False):
    """Prepare shared land and boundaries without importing the GB route network."""
    if not refresh and all((root / "prepared" / p).exists() for p in ["land.gpkg", "accounting.json"]):
        return
    from .prepare import normalise_boundary, normalise_land
    from .sources import download_all

    sources = download_all(config, root, refresh, include_osm=False)
    boundaries = {
        role: normalise_boundary(sources[role], role) for role in ["countries", "counties", "councils"]
    }
    country = boundaries["countries"].loc[boundaries["countries"].code.eq("E92000001")].geometry.union_all()
    if country.is_empty:
        raise ValueError("Reference boundaries contain no England geometry")
    land, accounting, rejected = normalise_land(sources, country)
    prepared = root / "prepared"
    prepared.mkdir(parents=True, exist_ok=True)
    tmp = prepared / "land.tmp.gpkg"
    tmp.unlink(missing_ok=True)
    for role, frame in boundaries.items():
        write_layer(frame, tmp, role)
    write_layer(land, tmp, "land")
    write_layer(rejected, tmp, "rejected")
    missing = {r["code"] for r in read_json(root / "coverage.json", {}).get("missing_authorities", [])}
    gaps = gpd.GeoDataFrame(
        pd.concat([f.loc[f.code.isin(missing)] for f in boundaries.values()], ignore_index=True), crs=CRS
    )
    write_layer(gaps, tmp, "coverage_gaps")
    os.replace(tmp, prepared / "land.gpkg")
    atomic_json(prepared / "accounting.json", accounting)
    # A previous complete route manifest must not describe changed land inputs.
    (prepared / "manifest.json").unlink(missing_ok=True)


def authority_table(root):
    frames = [
        pyogrio.read_dataframe(
            root / "prepared" / "land.gpkg", layer=role, columns=["code", "name"], read_geometry=False
        )
        for role in ["counties", "councils"]
    ]
    return pd.concat(frames).drop_duplicates("code").sort_values("name")


def resolve_region(root, config, county=None, region=None, bbox=None, name=None):
    presets = {**PRESETS, **config.get("regions", {})}
    spec = presets.get(region or "", {})
    if region and not spec:
        raise ValueError(f"Unknown region '{region}'. Configure it in [regions.{region}].")
    requested = county or spec.get("authorities", [])
    if isinstance(requested, str):
        requested = [requested]
    if not requested and not bbox:
        raise ValueError("Select --county, --region or --bbox for regional research")
    table = authority_table(root)
    codes, names = [], []
    for item in requested:
        found = table.loc[table.code.eq(item) | table.name.str.casefold().eq(item.casefold())]
        if found.empty and slugify(item) in presets:
            return resolve_region(root, config, region=slugify(item), name=name)
        if len(found) != 1:
            raise ValueError(f"Unknown or ambiguous county '{item}'; use its ONS code (see regions command)")
        codes.append(str(found.iloc[0].code))
        names.append(str(found.iloc[0]["name"]))
    geometries = []
    for role in ["counties", "councils"]:
        if codes:
            values = ",".join("'" + c.replace("'", "''") + "'" for c in codes)
            frame = pyogrio.read_dataframe(
                root / "prepared" / "land.gpkg", layer=role, where=f"code IN ({values})"
            )
            geometries.extend(frame.geometry)
    geometry = shapely.union_all(geometries) if geometries else box(*bbox)
    if bbox and geometries:
        geometry = geometry.intersection(box(*bbox))
    if geometry.is_empty:
        raise ValueError("Study area is empty")
    label = name or spec.get("name") or " + ".join(names) or "Custom region"
    preset = presets.get(slugify(label), {})
    spec = {**preset, **spec}
    return {
        **spec,
        "name": label,
        "slug": slugify(label),
        "authority_codes": sorted(set(codes)),
        "geometry": geometry,
        "bbox": list(geometry.bounds),
    }


def build_catalogue(root):
    directory = root / "catalogue"
    directory.mkdir(parents=True, exist_ok=True)
    with FileLock(directory / "catalogue.lock", timeout=3600):
        return _build_catalogue(root)


def _build_catalogue(root):
    from .analyse import form_sites

    land_path = root / "prepared" / "land.gpkg"
    if not land_path.exists():
        raise ValueError("Prepared reference land is missing; run reference first")
    directory = root / "catalogue"
    directory.mkdir(parents=True, exist_ok=True)
    land_hash = digest(land_path)
    signature = fingerprint(
        {
            "land": land_hash,
            "sites": inspect.getsource(form_sites),
        }
    )
    previous = read_json(directory / "manifest.json", {})
    if previous.get("signature") == signature and all(
        (directory / p).exists() for p in ["sites.gpkg", "membership.csv"]
    ):
        LOG.info("Reusing site catalogue (%d sites)", previous["sites"])
        return directory, previous
    land = pyogrio.read_dataframe(land_path, layer="land", use_arrow=True)
    LOG.info("Building shared site catalogue from %d components", len(land))
    sites, membership = form_sites(land)
    tmp = directory / "sites.tmp.gpkg"
    tmp.unlink(missing_ok=True)
    write_layer(sites, tmp, "sites")
    os.replace(tmp, directory / "sites.gpkg")
    pd.DataFrame(membership, columns=["site_id", "component_id"]).to_csv(
        directory / "membership.csv", index=False
    )
    value = {"signature": signature, "land_sha256": land_hash, "sites": len(sites), "components": len(land)}
    atomic_json(directory / "manifest.json", value)
    return directory, value


def read_poly(path):
    """Geofabrik polygon rings, including holes; coordinates are longitude/latitude."""
    lines = Path(path).read_text(encoding="utf-8").splitlines()[1:]
    shells, holes, current, is_hole = [], [], [], False
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        if line == "END":
            if current:
                (holes if is_hole else shells).append(Polygon(current))
                current = []
            continue
        fields = line.split()
        if len(fields) == 2:
            current.append(tuple(float(v) for v in fields))
        else:
            is_hole = line.startswith("!")
    geometry = shapely.union_all(shells).difference(shapely.union_all(holes))
    if geometry.is_empty or not geometry.is_valid:
        raise ValueError(f"Invalid extract coverage polygon: {path}")
    return gpd.GeoSeries([geometry], crs=4326).to_crs(CRS).iloc[0]


def regional_osm(root, spec, progress, refresh=False):
    import osmium

    from .prepare import RouteWriter, import_osm, import_signature
    from .sources import download_file, session

    directory = root / "regional_osm" / spec["slug"]
    directory.mkdir(parents=True, exist_ok=True)
    source = dict(spec.get("osm", {}))
    if source.get("path"):
        source.update(sha256=digest(Path(source["path"])), local=True)
        if not source.get("coverage_path"):
            raise ValueError("A local regional OSM source requires coverage_path (.poly or vector polygon)")
        coverage_path = Path(source["coverage_path"])
    else:
        slug = spec.get("osm_slug")
        if not slug and not source.get("url"):
            raise ValueError(
                "GB routes are not ready. Configure regional osm.url and coverage_url, or use an available preset."
            )
        url = source.get(
            "url", f"https://download.geofabrik.de/europe/united-kingdom/england/{slug}-latest.osm.pbf"
        )
        pbf = directory / "source.osm.pbf"
        with session() as client:
            source.update(download_file(client, url, pbf, refresh), path=str(pbf.resolve()))
            coverage_path = directory / "coverage.poly"
            coverage_url = source.get("coverage_url", url.replace("-latest.osm.pbf", ".poly"))
            source["coverage_metadata"] = download_file(client, coverage_url, coverage_path, refresh)
    coverage = (
        read_poly(coverage_path)
        if coverage_path.suffix == ".poly"
        else gpd.read_file(coverage_path).to_crs(CRS).geometry.union_all()
    )
    source["coverage_path"] = str(coverage_path.resolve())
    source["coverage_sha256"] = digest(coverage_path)
    source["attribution"] = "© OpenStreetMap contributors; ODbL 1.0"
    with osmium.io.Reader(source["path"], osmium.osm.osm_entity_bits.NOTHING) as reader:
        source["osm_snapshot"] = reader.header().get("osmosis_replication_timestamp")
    signature = fingerprint({"import": import_signature(source), "coverage": source["coverage_sha256"]})
    meta = read_json(directory / "manifest.json", {})
    if (
        refresh
        or meta.get("signature") != signature
        or not all((directory / p).exists() for p in ["routes.gpkg", "barriers.gpkg"])
    ):
        tmp = directory / "routes.tmp.gpkg"
        tmp.unlink(missing_ok=True)
        writer = RouteWriter(tmp)
        import_osm(Path(source["path"]), writer, directory / "barriers.gpkg", progress)
        if not writer.started:
            write_layer(
                empty_frame(
                    [
                        "route_index",
                        "source_id",
                        "tier",
                        "seed",
                        "start_node",
                        "end_node",
                        "grade",
                        "reason",
                        "name",
                    ]
                ),
                tmp,
                "routes",
            )
        os.replace(tmp, directory / "routes.gpkg")
        atomic_json(
            directory / "manifest.json",
            {"signature": signature, "sources": source, "route_count": writer.count},
        )
    return directory / "routes.gpkg", directory / "barriers.gpkg", source, coverage


def official_routes(spec, extent, role="prow", replacement=None):
    from .prepare import parts, vector_source

    path, layer = vector_source(spec, "prow")
    info = pyogrio.read_info(path, layer=layer)
    native_crs = info["crs"] or spec.get("crs")
    if not native_crs:
        raise ValueError("Official route source has no CRS")
    native_bounds = gpd.GeoSeries([extent], crs=CRS).to_crs(native_crs).total_bounds
    frame = pyogrio.read_dataframe(
        path, layer=layer, bbox=tuple(native_bounds), use_arrow=False, fid_as_index=True
    )
    if frame.crs is None:
        frame = frame.set_crs(native_crs)
    frame = frame.to_crs(CRS)
    records = []
    for fid, row in frame.iterrows():
        geom = shapely.force_2d(row.geometry) if row.geometry is not None else None
        if geom is None or geom.is_empty:
            raise ValueError(f"Empty official route geometry: {role}/{fid}")
        geom = shapely.make_valid(geom)
        if replacement is not None:
            geom = geom.difference(replacement)
        for part_no, line in enumerate(parts(geom, {"LineString"})):
            if line.length and line.intersects(extent):
                sid = row[spec["id_field"]] if spec.get("id_field") else f"fid:{fid}"
                records.append(
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
    return (
        gpd.GeoDataFrame(records, geometry="geometry", crs=CRS)
        if records
        else empty_frame(["source_id", "tier", "seed", "start_node", "end_node", "grade", "reason", "name"])
    )


def prepare_region(config, root, spec, buffer_m, progress, refresh=False):
    from .access_land import combined_catalogue, osm_commons

    catalogue, catalogue_meta = build_catalogue(root)
    catalogue, catalogue_meta = combined_catalogue(config, root, catalogue, catalogue_meta, refresh)
    study = spec["geometry"]
    candidates = pyogrio.read_dataframe(
        catalogue / "sites.gpkg", layer="sites", bbox=study.bounds, use_arrow=True
    )
    selected = candidates.loc[candidates.geometry.intersection(study).area.gt(0)].copy()
    extent = shapely.union_all([study, *selected.geometry]).buffer(buffer_m)
    baseline = read_json(root / "prepared" / "manifest.json", {})
    sources = dict(baseline.get("sources") or read_json(root / "sources.json", {}))
    if catalogue_meta.get("section15_source"):
        sources["section15"] = catalogue_meta["section15_source"]
    cached_regional = (root / "regional_osm" / spec["slug"] / "manifest.json").exists()
    global_ready = bool(baseline) and (root / "prepared" / "routes.gpkg").exists() and not cached_regional
    # Explicit regional source overrides take precedence over a shared GB snapshot.
    # Named pilots use small extracts for commons as well as cached route imports.
    if global_ready and not spec.get("osm") and not spec.get("osm_slug"):
        routes_path, barriers_path = root / "prepared" / "routes.gpkg", root / "prepared" / "barriers.gpkg"
        source_coverage = None
        route_signature = baseline["signature"]
    else:
        routes_path, barriers_path, sources["osm"], source_coverage = regional_osm(
            root, spec, progress, refresh
        )
        route_signature = sources["osm"]["sha256"]
    context = extent if source_coverage is None else extent.intersection(source_coverage)
    if context.is_empty:
        raise ValueError("Regional OSM coverage does not overlap the study area")
    context_sites = pyogrio.read_dataframe(
        catalogue / "sites.gpkg", layer="sites", bbox=context.bounds, use_arrow=True
    )
    context_sites = context_sites.loc[context_sites.intersects(context)]
    context_sites = (
        gpd.GeoDataFrame(pd.concat([context_sites, selected], ignore_index=True), crs=CRS)
        .drop_duplicates("site_id")
        .reset_index(drop=True)
    )
    region = {
        "name": spec["name"],
        "slug": spec["slug"],
        "authority_codes": spec["authority_codes"],
        "site_ids": selected.site_id.tolist(),
        "buffer_m": buffer_m,
        "context_limited_by_source": not context.equals(extent),
        "context_sha256": fingerprint(context.wkb_hex),
    }
    directory = root / "regions" / spec["slug"] / f"context-{buffer_m:g}"
    prepared = directory / "prepared"
    prepared.mkdir(parents=True, exist_ok=True)
    signature = fingerprint(
        {
            "catalogue": catalogue_meta["signature"],
            "routes": route_signature,
            "region": region,
            "code": digest(Path(__file__)),
            "overrides": [
                dict(x, sha256=source_digest(Path(x["path"]))) for x in config.get("overrides", [])
            ],
        }
    )
    previous = read_json(prepared / "manifest.json", {})
    if (
        not refresh
        and previous.get("signature") == signature
        and all((prepared / p).exists() for p in previous.get("files", []))
    ):
        LOG.info("Reusing %s input cache at %g m", spec["name"], buffer_m)
        return directory, region
    (prepared / "manifest.json").unlink(missing_ok=True)
    progress("select_routes", 0, None, area=spec["name"], buffer_m=buffer_m)
    routes = pyogrio.read_dataframe(routes_path, layer="routes", bbox=context.bounds, use_arrow=True)
    routes = routes.loc[routes.intersects(context)].copy()
    if routes_path != root / "prepared" / "routes.gpkg":
        replacements = [
            resolve_region(root, {}, county=[o["authority"]])["geometry"] for o in config.get("overrides", [])
        ]
        replacement = shapely.union_all(replacements) if replacements else None
        frames = [routes, official_routes(sources["prow"], context, replacement=replacement)]
    else:
        replacements = [
            resolve_region(root, {}, county=[o["authority"]])["geometry"] for o in config.get("overrides", [])
        ]
        if replacements:
            replacement = shapely.union_all(replacements)
            mask = routes.tier.eq("official")
            routes.loc[mask, "geometry"] = routes.loc[mask].geometry.difference(replacement)
            routes = routes.loc[~routes.geometry.is_empty].explode(index_parts=False).reset_index(drop=True)
        frames = [routes]
    for override, boundary in zip(config.get("overrides", []), replacements):
        extra = official_routes(override, context, role=f"council:{override['authority']}")
        if not extra.geometry.covered_by(boundary.buffer(5)).all():
            raise ValueError("Council replacement extends outside its declared authority")
        frames.append(extra)
        sources[f"council:{override['authority']}"] = {
            **override,
            "sha256": source_digest(Path(override["path"])),
            "local": True,
        }
    routes = gpd.GeoDataFrame(pd.concat(frames, ignore_index=True), crs=CRS).reset_index(drop=True)
    if "route_index" in routes:
        routes["original_route_index"] = routes.route_index
    routes["route_index"] = np.arange(len(routes), dtype="int64")
    progress("select_routes", len(routes), len(routes))
    tmp = prepared / "routes.tmp.gpkg"
    tmp.unlink(missing_ok=True)
    write_layer(routes, tmp, "routes")
    os.replace(tmp, prepared / "routes.gpkg")
    barriers = pyogrio.read_dataframe(barriers_path, layer="barriers", bbox=context.bounds, use_arrow=True)
    barriers = barriers.loc[barriers.intersects(context)]
    write_layer(barriers, prepared / "barriers.gpkg", "barriers")
    membership = pd.read_csv(catalogue / "membership.csv")
    membership = membership.loc[membership.site_id.isin(context_sites.site_id)]
    membership.to_csv(prepared / "membership.csv", index=False)
    land = pyogrio.read_dataframe(
        catalogue / "land.gpkg" if (catalogue / "land.gpkg").exists() else root / "prepared" / "land.gpkg",
        layer="land",
        bbox=tuple(context_sites.total_bounds) if len(context_sites) else context.bounds,
        use_arrow=True,
    )
    land = land.loc[land.component_id.isin(membership.component_id)]
    if len(land) != len(membership):
        raise ValueError("Regional site catalogue is missing source components")
    land_tmp = prepared / "land.tmp.gpkg"
    land_tmp.unlink(missing_ok=True)
    write_layer(land, land_tmp, "land")
    write_layer(context_sites, land_tmp, "sites")
    if sources.get("osm", {}).get("path") and (spec.get("osm_slug") or spec.get("osm")):
        commons_path = osm_commons(Path(sources["osm"]["path"]), routes_path.parent)
        hints = pyogrio.read_dataframe(commons_path, layer="unverified_commons", bbox=context.bounds)
        hints = hints.loc[hints.intersects(context)]
        write_layer(hints, land_tmp, "unverified_commons")
    for layer in ["counties", "councils", "coverage_gaps", "rejected"]:
        frame = pyogrio.read_dataframe(root / "prepared" / "land.gpkg", layer=layer, bbox=context.bounds)
        write_layer(frame, land_tmp, layer)
    write_layer(
        gpd.GeoDataFrame([{"name": spec["name"], "geometry": study}], crs=CRS), land_tmp, "study_area"
    )
    write_layer(gpd.GeoDataFrame([{"name": spec["name"], "geometry": context}], crs=CRS), land_tmp, "context")
    os.replace(land_tmp, prepared / "land.gpkg")
    accounting = read_json(root / "prepared" / "accounting.json", [])
    if (catalogue / "accounting.json").exists():
        accounting += read_json(catalogue / "accounting.json", [])
    source_ids = set(land.source_id)
    accounting = [r for r in accounting if r["source_id"] in source_ids or r["outcome"] == "rejected"]
    atomic_json(prepared / "accounting.json", accounting)
    atomic_json(directory / "coverage.json", read_json(root / "coverage.json", {}))
    # A possible component reaching the extraction edge cannot support a negative conclusion.
    edge = context.boundary.buffer(5)
    boundary_routes = routes.loc[routes.intersects(edge), "route_index"].astype(int).tolist()
    boundary_sites = context_sites.loc[~context_sites.geometry.covered_by(context), "site_id"].tolist()
    value = {
        "signature": signature,
        "route_count": len(routes),
        "land_components": len(land),
        "sources": sources,
        "synthetic": bool(baseline.get("synthetic")),
        "unmapped_missing_authorities": baseline.get("unmapped_missing_authorities", []),
        "region": region,
        "catalogue": True,
        "catalogue_signature": catalogue_meta["signature"],
        "boundary_routes": boundary_routes,
        "boundary_sites": boundary_sites,
        "files": ["land.gpkg", "routes.gpkg", "barriers.gpkg", "membership.csv", "accounting.json"],
    }
    if (catalogue / "lineage.csv").exists():
        lineage = pd.read_csv(catalogue / "lineage.csv")
        lineage.loc[lineage.site_id.isin(context_sites.site_id)].to_csv(prepared / "lineage.csv", index=False)
    atomic_json(prepared / "manifest.json", value)
    return directory, region


def research(config, root, output_base, reports, spec, refresh=False):
    output = output_base / spec["slug"]
    output.mkdir(parents=True, exist_ok=True)
    with FileLock(output / "research.lock", timeout=3600):
        return _research(config, root, output_base, reports, spec, refresh)


def _research(config, root, output_base, reports, spec, refresh=False):
    from .analyse import analyse
    from .export import export
    from .packages import preserve_completed, publish_completed
    from .reports import county_report

    published = output_base / spec["slug"]
    published.mkdir(parents=True, exist_ok=True)
    preserve_completed(published)
    output = published / "_working"
    output.mkdir(exist_ok=True)
    if (published / "review_sample.csv").exists():
        import shutil

        shutil.copy2(published / "review_sample.csv", output / "review_sample.csv")
    previous_review = output / "review_sample.csv"
    if previous_review.exists():
        review = pd.read_csv(previous_review).fillna("")
        fields = [
            f
            for f in ["site_id", "review_outcome", "review_notes", "review_source", "review_date"]
            if f in review
        ]
        reviewed = review.loc[review.review_outcome.ne("") | review.review_notes.ne(""), fields]
        if len(reviewed):
            config = {
                **config,
                "analysis": {
                    **config.get("analysis", {}),
                    "review_signature": fingerprint(reviewed.to_dict(orient="records")),
                },
            }
    progress = Progress(published / "progress.json")
    settings = config.get("analysis", {})
    buffer_m = float(settings.get("context_buffer_m", 20000))
    maximum = float(settings.get("max_context_buffer_m", 80000))
    if buffer_m <= 0 or maximum < buffer_m:
        raise ValueError("Context buffers must be positive and maximum >= initial")
    previous_context = None
    while True:
        local_root, region = prepare_region(config, root, spec, buffer_m, progress, refresh)
        if region["context_sha256"] == previous_context:
            break
        previous_context = region["context_sha256"]
        value = analyse(config, local_root, output, refresh=refresh, progress=progress)
        sites = pyogrio.read_dataframe(output / "results.gpkg", layer="sites")
        unresolved = sites.reasons.str.contains("regional_context_incomplete") & ~sites.status.isin(
            ["access_evidenced", "permissive_access_evidenced"]
        )
        if not unresolved.any() or buffer_m >= maximum or region["context_limited_by_source"]:
            break
        LOG.info("%d sites reach the context edge; expanding surrounding network", unresolved.sum())
        buffer_m = min(maximum, buffer_m * 2)
    progress("export", 0, len(sites))
    report = export(output)
    progress("validate", 0, len(sites))
    table = pd.read_csv(output / "sites.csv")
    members = pd.read_csv(output / "site_membership.csv")
    if (
        set(table.site_id) != set(sites.site_id)
        or not table.site_id.is_unique
        or set(members.site_id) != set(sites.site_id)
    ):
        raise ValueError("Regional CSV, GIS and membership IDs differ")
    points = gpd.GeoSeries(gpd.points_from_xy(sites.easting, sites.northing), crs=CRS)
    candidate_json = read_json(output / "likely_access_islands.geojson")
    expected_candidates = set(sites.loc[sites.status.eq("likely_access_island"), "site_id"])
    if {f["properties"]["site_id"] for f in candidate_json["features"]} != expected_candidates:
        raise ValueError("Regional candidate GeoJSON and GIS IDs differ")
    permitted = pd.read_csv(output / "permissive_access_evidenced.csv")
    if set(permitted.site_id) != set(sites.loc[sites.status.eq("permissive_access_evidenced"), "site_id"]):
        raise ValueError("Regional permissive CSV and GIS IDs differ")
    if set(sites.site_id) != set(region["site_ids"]):
        raise ValueError("Regional output does not cover its complete selected site inventory")
    if not sites.geometry.is_valid.all() or not points.covered_by(sites.geometry).all():
        raise ValueError("Regional geometry validation failed")
    atomic_json(
        output / "validation.json",
        {
            "complete": True,
            "analysis_signature": value["signature"],
            "sites": len(sites),
            "matching_ids": True,
            "valid_geometry": True,
        },
    )
    progress("finished", len(sites), len(sites), complete=bool(value["complete"]))
    atomic_json(output / "progress.json", progress.state)
    if value["complete"]:
        package = publish_completed(output, published)
        path = county_report(package, reports)
        report["county_report"] = str(path.resolve())
    return report
