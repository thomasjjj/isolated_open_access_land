"""Supplemental statutory land and explicitly unverified OSM common areas.

Kept separate from route preparation so adding land does not invalidate PBF imports.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import geopandas as gpd
import pandas as pd
import pyogrio
import shapely
from filelock import FileLock

from .common import CRS, UnionFind, atomic_json, digest, empty_frame, fingerprint, read_json, write_layer

LOG = logging.getLogger(__name__)
SECTION15_CATALOGUE = "72eda560-eee8-4667-a588-b5289c4ae011"
SECTION15_FILESET = "da6c5f5f-7147-4f2e-96cc-a718bddf0022"
SECTION15_URL = (
    "https://environment.data.gov.uk/api/file/download?fileDataSetId="
    + SECTION15_FILESET
    + "&fileName=CRoW_Act_2000_Section_15_Land.gpkg.zip"
)


def section15_source(config, root, refresh=False):
    from .sources import download_file, extract_archive, get_json, session

    configured = config.get("sources", {}).get("section15", {})
    if configured.get("path"):
        return {**configured, "sha256": digest(Path(configured["path"])), "local": True}
    directory = root / "supplemental" / "section15"
    directory.mkdir(parents=True, exist_ok=True)
    previous = read_json(directory / "source.json", {})
    if not refresh and previous and Path(previous["path"]).exists():
        previous.setdefault("version", previous.get("catalogue_modified"))
        return previous
    with session() as client:
        catalog = get_json(
            client,
            "https://environment.data.gov.uk/backend/catalog/api/catalog/data-sets/" + SECTION15_CATALOGUE,
        )
        downloaded = download_file(
            client, configured.get("url", SECTION15_URL), directory / "source.zip", refresh
        )
    extract_archive(directory / "source.zip", directory / "vector")
    paths = sorted((directory / "vector").rglob("*.gpkg"))
    if len(paths) != 1:
        raise ValueError("Expected one section 15 GeoPackage")
    source = {
        **downloaded,
        "path": str(paths[0].resolve()),
        "layer": "Crow_Act_2000_Section_15_Land_England",
        "catalogue_url": "https://environment.data.gov.uk/dataset/" + SECTION15_CATALOGUE,
        "catalogue_modified": catalog.get("modified"),
        "version": catalog.get("modified"),
        "attribution": "© Natural England copyright. Contains Ordnance Survey data © Crown copyright and database right. Open Government Licence v3.",
        "currency_note": "Retrieval and file publication dates are not survey dates; pre-existing statutory rights vary by Act.",
    }
    atomic_json(directory / "source.json", source)
    return source


def normalise_section15(source, england):
    from .prepare import CountryClipper, parts, vector_source

    path, layer = vector_source(source, "section15")
    frame = pyogrio.read_dataframe(path, layer=layer, fid_as_index=True).to_crs(CRS)
    if frame.empty:
        raise ValueError("Required section 15 source is empty")
    clipper = CountryClipper(england)
    records, accounting, rejected = [], [], []
    for fid, row in frame.iterrows():
        sid = f"section15:fid:{fid}"
        geometry = row.geometry
        original = shapely.force_2d(geometry) if geometry is not None else None
        polygons = parts(shapely.make_valid(original), {"Polygon"}) if original is not None else []
        if not polygons:
            accounting.append({"source_id": sid, "component": 0, "outcome": "rejected"})
            rejected.append({"source_id": sid, "reason": "no_polygon_geometry", "geometry": original})
            continue
        attrs = {c: None if pd.isna(row[c]) else str(row[c]) for c in frame.columns if c != "geometry"}
        for number, polygon in enumerate(polygons):
            clipped = parts(clipper.clip(polygon), {"Polygon"})
            accounting.append(
                {
                    "source_id": sid,
                    "component": number,
                    "outcome": "processed" if clipped else "outside_england",
                }
            )
            for part_no, part in enumerate(clipped):
                records.append(
                    {
                        "component_id": f"{sid}:{number}:{part_no}",
                        "source_id": sid,
                        "name": attrs.get("common") or "",
                        "source_attributes": json.dumps(attrs, sort_keys=True),
                        "geometry": part,
                    }
                )
    columns = ["component_id", "source_id", "name", "source_attributes"]
    land = gpd.GeoDataFrame(records, crs=CRS) if records else empty_frame(columns)
    rejects = gpd.GeoDataFrame(rejected, crs=CRS) if rejected else empty_frame(["source_id", "reason"])
    return land, accounting, rejects


def legal_metadata(land):
    land = land.copy()
    regimes, common_flags = [], []
    for row in land.itertuples():
        attrs = json.loads(getattr(row, "source_attributes", "{}") or "{}")
        attrs = {str(k).lower(): str(v or "").strip() for k, v in attrs.items()}
        section15 = row.source_id.startswith("section15:")
        common = (
            attrs.get("rcl", "").lower() in {"yes", "true", "1"} or attrs.get("rcl_oc", "").upper() == "RCL"
        )
        values = []
        if common:
            values.append("registered_common_land")
        if attrs.get("oc", "").lower() in {"yes", "true", "1"}:
            values.append("open_country")
        if row.source_id.startswith("dedicated:") or attrs.get("s16", "").lower() in {"yes", "true", "1"}:
            values.append("section16_dedication")
        if section15:
            values.append("section15_statutory_access")
        if not values:
            values.append("crow_access_land")
        regimes.append(";".join(values))
        common_flags.append(common)
    land["access_regimes"], land["registered_common_land"] = regimes, common_flags
    return land


def annotate_sites(sites, land, membership):
    sites = sites.copy()
    source = legal_metadata(land).set_index("component_id")
    groups = pd.DataFrame(membership).groupby("site_id").component_id.apply(list)
    regimes, flags, attributes = [], [], []
    for sid in sites.site_id:
        rows = source.loc[groups[sid]]
        regimes.append(";".join(sorted({v for raw in rows.access_regimes for v in raw.split(";")})))
        flags.append(bool(rows.registered_common_land.any()))
        raw = rows.source_attributes if "source_attributes" in rows else pd.Series("{}", index=rows.index)
        attributes.append(json.dumps(dict(zip(rows.source_id, raw.map(json.loads))), sort_keys=True))
    sites["access_regimes"], sites["registered_common_land"], sites["legal_source_attributes"] = (
        regimes,
        flags,
        attributes,
    )
    return sites


def reconcile_ids(sites, legacy):
    """Reuse an ID only for a topologically identical complete geometry."""
    sites = sites.copy()
    replacements, lineage = {}, []
    tree = legacy.sindex
    legacy_ids = set(legacy.site_id)
    old_indices = dict(zip(legacy.site_id, range(len(legacy))))
    for i, row in sites.iterrows():
        if "previous_site_ids" in sites:
            included = [old_indices[sid] for sid in row.previous_site_ids.split(";") if sid]
            equal = []
            if len(included) == 1:
                old = legacy.geometry.iloc[included[0]]
                if row.geometry.equals_exact(old, 0, normalize=True) or row.geometry.equals(old):
                    equal = included
                    replacements[row.site_id] = legacy.site_id.iloc[included[0]]
                    sites.at[i, "site_id"] = replacements[row.site_id]
            if not equal and row.site_id in legacy_ids:
                replacements[row.site_id] = (
                    "site-" + fingerprint([row.site_id, shapely.normalize(row.geometry).wkb_hex])[:16]
                )
                sites.at[i, "site_id"] = replacements[row.site_id]
            for j in included:
                lineage.append(
                    {
                        "previous_site_id": legacy.site_id.iloc[j],
                        "site_id": sites.at[i, "site_id"],
                        "relationship": "unchanged_geometry" if j in equal else "changed_geometry",
                        "overlap_m2": legacy.geometry.iloc[j].area,
                    }
                )
            continue
        direct = old_indices.get(row.site_id)
        if direct is not None and row.geometry.equals_exact(legacy.geometry.iloc[direct], 0, normalize=True):
            lineage.append(
                {
                    "previous_site_id": row.site_id,
                    "site_id": row.site_id,
                    "relationship": "unchanged_geometry",
                    "overlap_m2": row.geometry.area,
                }
            )
            continue
        previous = [
            int(j)
            for j in tree.query(row.geometry, predicate="intersects")
            if row.geometry.intersection(legacy.geometry.iloc[j]).area > 0
        ]
        equal = [j for j in previous if row.geometry.equals(legacy.geometry.iloc[j])]
        if len(equal) == 1:
            replacements[row.site_id] = legacy.site_id.iloc[equal[0]]
            sites.at[i, "site_id"] = replacements[row.site_id]
        elif row.site_id in legacy_ids:
            replacements[row.site_id] = (
                "site-" + fingerprint([row.site_id, shapely.normalize(row.geometry).wkb_hex])[:16]
            )
            sites.at[i, "site_id"] = replacements[row.site_id]
        for j in previous:
            lineage.append(
                {
                    "previous_site_id": legacy.site_id.iloc[j],
                    "site_id": sites.at[i, "site_id"],
                    "relationship": "unchanged_geometry" if j in equal else "changed_geometry",
                    "overlap_m2": row.geometry.intersection(legacy.geometry.iloc[j]).area,
                }
            )
    if not sites.site_id.is_unique:
        raise ValueError("Reconciled site IDs are not unique")
    return sites, replacements, lineage


def extend_sites(legacy, legacy_membership, supplemental, land):
    """Only new polygons need connectivity tests; legacy sites are already united."""
    nodes = gpd.GeoDataFrame(
        pd.concat([legacy[["geometry"]], supplemental[["geometry"]]], ignore_index=True), crs=CRS
    )
    uf, tree = UnionFind(len(nodes)), nodes.sindex
    for i in range(len(legacy), len(nodes)):
        geometry = nodes.geometry.iloc[i]
        for j in tree.query(geometry, predicate="intersects"):
            j = int(j)
            if j >= i or uf.find(i) == uf.find(j):
                continue
            overlap = geometry.intersection(nodes.geometry.iloc[j])
            if overlap.area > 0 or overlap.length > 0:
                uf.union(i, j)
        if (i - len(legacy)) % 500 == 0:
            LOG.info("Section 15 connectivity: %d / %d components", i - len(legacy), len(supplemental))
    groups = {}
    for i in range(len(nodes)):
        groups.setdefault(uf.find(i), []).append(i)
    old_members = legacy_membership.groupby("site_id").component_id.apply(list).to_dict()
    source = land.set_index("component_id")
    records, membership = [], []
    for indices in groups.values():
        members = []
        for i in indices:
            members.extend(
                old_members[legacy.site_id.iloc[i]]
                if i < len(legacy)
                else [supplemental.component_id.iloc[i - len(legacy)]]
            )
        members.sort()
        sid = "site-" + fingerprint(members)[:16]
        subset = source.loc[members]
        names = sorted({str(x) for x in subset["name"] if str(x)})
        geometry = (
            nodes.geometry.iloc[indices[0]] if len(indices) == 1 else nodes.geometry.iloc[indices].union_all()
        )
        records.append(
            {
                "site_id": sid,
                "previous_site_ids": ";".join(legacy.site_id.iloc[i] for i in indices if i < len(legacy)),
                "name": "; ".join(names) or f"Unnamed access land {sid[5:13]}",
                "source_ids": ";".join(sorted(set(subset.source_id))),
                "parcel_count": len(set(subset.source_id)),
                "component_count": len(members),
                "area_ha": geometry.area / 10000,
                "geometry": geometry,
            }
        )
        membership.extend({"site_id": sid, "component_id": m} for m in members)
    return gpd.GeoDataFrame(records, crs=CRS).sort_values("site_id").reset_index(drop=True), membership


def combined_catalogue(config, root, legacy_directory, legacy_meta, refresh=False):

    # Offline synthetic/custom fixtures keep their existing source scope unless opted in.
    real_sources = read_json(root / "sources.json", {})
    enabled = config.get("analysis", {}).get(
        "include_section15", bool(real_sources) or bool(config.get("sources", {}).get("section15"))
    )
    if not enabled:
        return legacy_directory, legacy_meta
    source = section15_source(config, root, refresh)
    signature = fingerprint(
        {"legacy": legacy_meta["signature"], "section15": source["sha256"], "code": digest(Path(__file__))}
    )
    directory = root / "catalogue" / "combined" / signature
    directory.mkdir(parents=True, exist_ok=True)
    with FileLock(directory / "catalogue.lock", timeout=3600):
        previous = read_json(directory / "manifest.json", {})
        if previous and all(
            (directory / p).exists() for p in ["sites.gpkg", "land.gpkg", "membership.csv", "lineage.csv"]
        ):
            return directory, previous
        path = root / "prepared" / "land.gpkg"
        land = pyogrio.read_dataframe(path, layer="land", use_arrow=True)
        england = pyogrio.read_dataframe(path, layer="countries").geometry.union_all()
        supplemental, accounting, rejected = normalise_section15(source, england)
        land = gpd.GeoDataFrame(pd.concat([land, supplemental], ignore_index=True), crs=CRS)
        LOG.info("Grouping %d CRoW and section 15 components", len(land))
        legacy = pyogrio.read_dataframe(legacy_directory / "sites.gpkg", layer="sites", use_arrow=True)
        sites, membership = extend_sites(
            legacy, pd.read_csv(legacy_directory / "membership.csv"), supplemental, land
        )
        sites, replacements, lineage = reconcile_ids(sites, legacy)
        for row in membership:
            row["site_id"] = replacements.get(row["site_id"], row["site_id"])
        sites = annotate_sites(sites, land, membership)
        write_layer(legal_metadata(land), directory / "land.gpkg", "land")
        write_layer(rejected, directory / "land.gpkg", "rejected")
        write_layer(sites, directory / "sites.gpkg", "sites")
        pd.DataFrame(membership).to_csv(directory / "membership.csv", index=False)
        pd.DataFrame(lineage, columns=["previous_site_id", "site_id", "relationship", "overlap_m2"]).to_csv(
            directory / "lineage.csv", index=False
        )
        atomic_json(directory / "accounting.json", accounting)
        meta = {
            "signature": signature,
            "sites": len(sites),
            "components": len(land),
            "section15_source": source,
            "section15_components": len(supplemental),
            "section15_rejected": len(rejected),
        }
        atomic_json(directory / "manifest.json", meta)
        return directory, meta


def common_tag(tags):
    return any(tags.get(key) == "common" for key in ["designation", "leisure", "landuse"])


def osm_commons(pbf, directory):
    """Assemble ways and multipolygon relations; a tag supplies no statutory right."""
    import osmium
    from pyproj import Transformer
    from shapely.ops import transform

    signature = fingerprint({"source": digest(pbf), "code": digest(Path(__file__))})
    path = directory / "commons.gpkg"
    if read_json(directory / "commons.json", {}).get("signature") == signature and path.exists():
        return path
    factory = osmium.geom.WKBFactory()
    project = Transformer.from_crs(4326, CRS, always_xy=True).transform
    records, failures = [], []

    class Areas(osmium.SimpleHandler):
        def area(self, area):
            tags = dict(area.tags)
            if not common_tag(tags):
                return
            source_id = f"osm:{'way' if area.from_way() else 'relation'}:{area.orig_id()}"
            try:
                geometry = transform(project, shapely.from_wkb(factory.create_multipolygon(area)))
                geometry = shapely.make_valid(geometry)
                if geometry.is_empty or geometry.area == 0:
                    raise ValueError("No area geometry")
                records.append(
                    {
                        "source_id": source_id,
                        "site_id": "hint:" + source_id,
                        "name": tags.get("name", ""),
                        "access": tags.get("access", ""),
                        "foot": tags.get("foot", ""),
                        "source_attributes": json.dumps(tags, sort_keys=True),
                        "verification": "unverified_osm_common",
                        "geometry": geometry,
                    }
                )
            except (RuntimeError, ValueError) as error:
                failures.append({"source_id": source_id, "reason": str(error)})

    Areas().apply_file(str(pbf), locations=True, idx="flex_mem")
    frame = (
        gpd.GeoDataFrame(records, crs=CRS)
        if records
        else empty_frame(
            ["source_id", "site_id", "name", "access", "foot", "source_attributes", "verification"]
        )
    )
    tmp = path.with_suffix(".tmp.gpkg")
    tmp.unlink(missing_ok=True)
    write_layer(frame.drop_duplicates("source_id"), tmp, "unverified_commons")
    os.replace(tmp, path)
    atomic_json(
        directory / "commons.json", {"signature": signature, "areas": len(frame), "rejected": failures}
    )
    return path
