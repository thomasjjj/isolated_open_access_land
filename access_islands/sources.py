"""Cached downloads and strict, paginated feature-service acquisition."""

from __future__ import annotations

import logging
import os
import re
import time
import zipfile
from pathlib import Path
from urllib.parse import quote

import geopandas as gpd
import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .common import atomic_json, digest, now, read_json, source_digest

LOG = logging.getLogger(__name__)
DSP = "https://environment.data.gov.uk/backend/catalog/api"
GUIDE = "https://designatedsites.naturalengland.org.uk/GreenInfrastructure/UserGuide/GreenInfrastructureModules.aspx"
CATALOGUES = {
    "land": "0896250c-3aaa-43e2-b783-a133522cb5e3",
    "dedicated": "2bc7c6e4-30dc-429a-a673-2b7145b54173",
    "prow": "89b5ad77-8d08-4036-a494-d3b1a1fd758c",
}
BOUNDARIES = {
    "councils": "Local_Authority_Districts_DEC_2025_Boundaries_UK_BFC",
    "counties": "Counties_and_Unitary_Authorities_December_2025_Boundaries_UK_BFC",
    "countries": "Countries_December_2025_Boundaries_UK_BFC",
}


def session():
    s = requests.Session()
    s.headers["User-Agent"] = "access-islands/0.2 (England open-access research)"
    retry = Retry(total=4, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])
    s.mount("https://", HTTPAdapter(max_retries=retry))
    return s


def get_json(s, url, **params):
    r = s.get(url, params=params, timeout=(30, 120))
    r.raise_for_status()
    j = r.json()
    if "error" in j:
        raise ValueError(f"Feature service error at {url}: {j['error']}")
    return j


def download_file(s, url: str, target: Path, refresh=False):
    meta_path = target.with_suffix(target.suffix + ".json")
    old = read_json(meta_path, {})
    if not refresh and target.exists() and old.get("url") == url:
        if digest(target) == old.get("sha256"):
            return old
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_suffix(target.suffix + ".part")
    partial_meta = partial.with_suffix(partial.suffix + ".json")
    for attempt in range(4):
        saved = read_json(partial_meta, {})
        offset = partial.stat().st_size if partial.exists() and saved.get("url") == url else 0
        headers = {"Accept-Encoding": "identity"}
        if offset:
            headers["Range"] = f"bytes={offset}-"
        if offset and saved.get("etag"):
            headers["If-Range"] = saved["etag"]
        try:
            with s.get(url, headers=headers, stream=True, timeout=(30, 120)) as r:
                if r.status_code == 416:
                    partial.unlink(missing_ok=True)
                    continue
                r.raise_for_status()
                if target.name.endswith(".osm.pbf") and "text/html" in r.headers.get("Content-Type", ""):
                    raise ValueError(f"OSM URL returned an HTML page: {r.url}")
                if offset and r.status_code != 206:
                    offset = 0
                if r.status_code == 206 and not r.headers.get("Content-Range", "").startswith(
                    f"bytes {offset}-"
                ):
                    raise ValueError("Server returned an unexpected resume range")
                atomic_json(partial_meta, {"url": url, "etag": r.headers.get("ETag")})
                expected = (
                    int(r.headers.get("Content-Length", 0)) if not r.headers.get("Content-Encoding") else 0
                )
                count, reported = 0, time.monotonic()
                with partial.open("ab" if offset else "wb") as f:
                    for chunk in r.iter_content(1024 * 1024):
                        f.write(chunk)
                        count += len(chunk)
                        if time.monotonic() - reported > 15:
                            LOG.info("Downloading %s: %.0f MiB", target.name, (offset + count) / 1048576)
                            reported = time.monotonic()
                if expected and count != expected:
                    raise ValueError(f"Truncated download: {count} of {expected} bytes")
                if not partial.stat().st_size:
                    raise ValueError("Empty download")
                if zipfile.is_zipfile(partial):
                    with zipfile.ZipFile(partial) as z:
                        bad = z.testzip()
                        if bad:
                            raise ValueError(f"Corrupt ZIP member: {bad}")
                os.replace(partial, target)
                partial_meta.unlink(missing_ok=True)
                meta = {
                    "url": url,
                    "retrieved": now(),
                    "sha256": digest(target),
                    "bytes": target.stat().st_size,
                    "etag": r.headers.get("ETag"),
                    "last_modified": r.headers.get("Last-Modified"),
                }
                atomic_json(meta_path, meta)
                return meta
        except (requests.RequestException, ValueError):
            if attempt == 3:
                raise
            time.sleep(2**attempt)
    raise ValueError(f"Cannot complete download: {url}")


def extract_archive(archive: Path, target: Path):
    """Validate every path before extraction; prevent traversal through downloaded ZIPs."""
    stamp = target / "archive.json"
    h = digest(archive)
    if read_json(stamp, {}).get("sha256") == h:
        return
    target.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as z:
        for item in z.infolist():
            resolved = (target / item.filename).resolve()
            if not resolved.is_relative_to(target.resolve()):
                raise ValueError(f"Unsafe archive path: {item.filename}")
        z.extractall(target)
    atomic_json(stamp, {"sha256": h})


def acquire_arcgis(s, url: str, target: Path, refresh=False, where="1=1"):
    url = url.rstrip("/")
    old = read_json(target.with_suffix(".json"), {})
    info = get_json(s, url, f="json")
    version = info.get("editingInfo", {}).get("lastEditDate")
    if (
        not refresh
        and target.exists()
        and old.get("url") == url
        and old.get("version") == version
        and old.get("where", "1=1") == where
    ):
        if digest(target) == old.get("sha256"):
            return old
    query = url + "/query"
    ids = get_json(s, query, where=where, returnIdsOnly="true", f="json").get("objectIds")
    if ids is None:
        raise ValueError(f"Service did not supply object IDs: {url}")
    if not ids:
        raise ValueError(f"Required boundary service is empty: {url}")
    ids = sorted(ids)
    tmp = target.with_suffix(".tmp.gpkg")
    tmp.unlink(missing_ok=True)
    received = set()
    id_field = info["objectIdField"]
    chunk_size = min(10, int(info.get("maxRecordCount", 1000)))

    def feature_batches(batch):
        try:
            result = get_json(
                s,
                query,
                objectIds=",".join(map(str, batch)),
                outFields="*",
                returnGeometry="true",
                outSR=4326,
                geometryPrecision=6,
                f="geojson",
            )
            if result.get("exceededTransferLimit"):
                raise requests.RequestException("Transfer limit exceeded")
        except requests.RequestException:
            if len(batch) <= 1:
                raise
            mid = len(batch) // 2
            yield from feature_batches(batch[:mid])
            yield from feature_batches(batch[mid:])
            return
        yield batch, result

    try:
        for start in range(0, len(ids), chunk_size):
            for batch, j in feature_batches(ids[start : start + chunk_size]):
                features = j.get("features", [])
                got = {f["properties"][id_field] for f in features}
                if got != set(batch):
                    raise ValueError(f"Missing feature IDs from {url}: {set(batch) - got}")
                append = bool(received)
                received.update(got)
                frame = gpd.GeoDataFrame.from_features(features, crs=4326)
                # OGR reserves fid as its internal primary key; keep service IDs as normal fields.
                frame = frame.rename(columns={c: "source_fid" for c in frame.columns if c.lower() == "fid"})
                frame.to_file(
                    tmp,
                    layer="features",
                    driver="GPKG",
                    engine="pyogrio",
                    append=append,
                    promote_to_multi=True,
                )
                LOG.info("%s: %d/%d features", target.stem, len(received), len(ids))
        if received != set(ids):
            raise ValueError("Incomplete feature service acquisition")
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)
    meta = {
        "url": url,
        "where": where,
        "retrieved": now(),
        "version": version,
        "sha256": digest(target),
        "feature_count": len(ids),
        "attribution": info.get("copyrightText", ""),
    }
    atomic_json(target.with_suffix(".json"), meta)
    return meta


def resolve_defra(s, role):
    entry = get_json(s, f"{DSP}/catalog/data-sets/{CATALOGUES[role]}")
    file_id = entry["dataSet"]["id"]
    files = get_json(s, f"{DSP}/file-management/data-sets/{file_id}")["files"]
    files = [{**f, "fileName": f.get("fileName", f.get("name"))} for f in files]
    # Never choose the density/higher-rights layer instead of the actual network.
    preferred = [f for f in files if f["fileName"].lower().endswith(".gpkg.zip")]
    if not preferred:
        preferred = [f for f in files if f["fileName"].lower().endswith((".gdb.zip", ".shp.zip", ".zip"))]
    if role == "prow":
        preferred = [f for f in preferred if not re.search("density|higher|terrain", f["fileName"], re.I)]
    if len(preferred) != 1:
        raise ValueError(
            f"Cannot choose {role} download from {[f['fileName'] for f in files]}; set a URL in config"
        )
    filename = preferred[0]["fileName"]
    url = f"https://environment.data.gov.uk/api/file/download?fileDataSetId={file_id}&fileName={quote(filename)}"
    return url, entry


def coverage(s, target: Path):
    r = s.get(GUIDE, timeout=(30, 120))
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    table = next((t for t in soup.find_all("table") if "Middlesbrough" in t.get_text()), None)
    if table is None:
        raise ValueError("Could not find the published rights-of-way coverage table")
    rows = []
    for row in table.find_all("tr"):
        cells = [c.get_text(" ", strip=True) for c in row.find_all(["td", "th"])]
        if len(cells) >= 2 and re.fullmatch(r"E\d{8}", cells[0]):
            rows.append({"code": cells[0], "name": cells[1]})
    if not rows:
        raise ValueError("Empty rights-of-way coverage table")
    value = {"url": GUIDE, "retrieved": now(), "missing_authorities": rows}
    atomic_json(target, value)
    return value


def download_all(config: dict, root: Path, refresh=False, include_osm=True):
    raw = root / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    s = session()
    manifest = read_json(root / "sources.json", {})
    for role in ["land", "dedicated", "prow", "countries", "counties", "councils", "osm"]:
        if role == "osm" and not include_osm:
            continue
        spec = config.get("sources", {}).get(role, {})
        if spec.get("path"):
            p = Path(spec["path"]).resolve()
            if not p.exists():
                raise FileNotFoundError(p)
            previous = manifest.get(role, {})
            h = source_digest(p)
            manifest[role] = {
                **spec,
                "path": str(p),
                "sha256": h,
                "retrieved": previous.get("retrieved", now())
                if previous.get("sha256") == h and previous.get("path") == str(p)
                else now(),
                "local": True,
            }
        elif role in CATALOGUES:
            url, entry = (spec["url"], {}) if spec.get("url") else resolve_defra(s, role)
            target = raw / f"{role}.zip"
            meta = download_file(s, url, target, refresh)
            directory = raw / role
            extract_archive(target, directory)
            manifest[role] = {
                **spec,
                **meta,
                "path": str(directory.resolve()),
                "version": entry.get("modified"),
                "attribution": entry.get("licence", {}),
            }
        elif role in BOUNDARIES:
            url = spec.get("url") or (
                "https://services1.arcgis.com/ESMARspQHYMw9BZ9/arcgis/rest/services/"
                + BOUNDARIES[role]
                + "/FeatureServer/0"
            )
            p = raw / f"{role}.gpkg"
            field = {"countries": "CTRY25CD", "counties": "CTYUA25CD", "councils": "LAD25CD"}[role]
            where = spec.get("where", f"{field} LIKE 'E%'" if not spec.get("url") else "1=1")
            manifest[role] = {**spec, **acquire_arcgis(s, url, p, refresh, where), "path": str(p.resolve())}
        else:
            p = raw / "great-britain.osm.pbf"
            url = spec.get("url", "https://download.geofabrik.de/europe/great-britain-latest.osm.pbf")
            manifest[role] = {
                **spec,
                **download_file(s, url, p, refresh),
                "path": str(p.resolve()),
                "attribution": "© OpenStreetMap contributors; ODbL 1.0",
            }
            import osmium

            with osmium.io.Reader(str(p)) as reader:
                header = reader.header()
                manifest[role]["osm_snapshot"] = header.get("osmosis_replication_timestamp") or header.get(
                    "timestamp"
                )
        atomic_json(root / "sources.json", manifest)
    if config.get("coverage", {}).get("path"):
        value = read_json(Path(config["coverage"]["path"]))
        if not isinstance(value, dict) or "missing_authorities" not in value:
            raise ValueError("Coverage JSON requires missing_authorities")
        atomic_json(root / "coverage.json", value)
    else:
        coverage(s, root / "coverage.json")
    return manifest
