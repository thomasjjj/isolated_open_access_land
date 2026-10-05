"""Named Markdown research reports with offline geographic map images."""

from __future__ import annotations

import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit, urlunsplit

import pandas as pd
import pyogrio
from filelock import FileLock

from .cartography import COLOURS, STYLE_VERSION, load_context, plot_map
from .common import atomic_json, code_signature, fingerprint, now, read_json
from .export import atomic_text


def ordered_candidates(sites):
    return sites.loc[sites.status.eq("likely_access_island")].sort_values(
        ["area_ha", "site_id"], ascending=[False, True]
    )


def source_version(source):
    version = (
        source.get("osm_snapshot")
        or source.get("version")
        or source.get("last_modified")
        or source.get("sha256", "")[:12]
    )
    # ArcGIS catalogue versions commonly use milliseconds since the Unix epoch.
    if str(version).isdigit() and 1e12 <= int(version) < 1e13:
        return datetime.fromtimestamp(int(version) / 1000, timezone.utc).isoformat()
    return version


def relative_link(target, parent):
    return quote(Path(os.path.relpath(target, parent)).as_posix(), safe="/.-_")


def preserve_previous_report(reports, path, slug, signature, county_root, presentation_signature=None):
    previous = read_json(reports / "index.json", {}).get(slug, {})
    old_signature = previous.get("analysis_signature")
    old_presentation = previous.get("presentation_signature")
    same_presentation = presentation_signature is None or old_presentation == presentation_signature
    if not path.exists() or not old_signature or (old_signature == signature and same_presentation):
        return
    history = reports / "history" / slug / old_signature
    if old_signature == signature:
        history = history / ("presentation-" + (old_presentation or "legacy"))
    archived = history / path.name
    if archived.exists():
        return
    history.mkdir(parents=True, exist_ok=True)
    for image in (reports / "maps").glob(f"{slug}-*"):
        if not image.is_file() or image.suffix not in {".png", ".svg", ".json"}:
            continue
        (history / "maps").mkdir(exist_ok=True)
        shutil.copy2(image, history / "maps" / image.name)

    def rewrite(match):
        parts = urlsplit(match[1])
        if parts.scheme or parts.netloc:
            return match[0]
        target = (reports / unquote(parts.path)).resolve()
        try:
            relative = target.relative_to(county_root)
            frozen = county_root / "runs" / old_signature / relative
            if frozen.exists():
                target = frozen
        except ValueError:
            pass
        if target.parent == reports / "maps":
            target = history / "maps" / target.name
        return "](" + urlunsplit(("", "", relative_link(target, history), parts.query, parts.fragment)) + ")"

    atomic_text(archived, re.sub(r"\]\(([^)]+)\)", rewrite, path.read_text(encoding="utf-8")))


def county_report(output, reports):
    from .packages import report_package

    output = report_package(output)
    manifest = read_json(output / "run_manifest.json", {})
    validation = read_json(output / "validation.json", {})
    region = manifest.get("scope", {}).get("region")
    if (
        not region
        or not manifest.get("complete")
        or not validation.get("complete")
        or validation.get("analysis_signature") != manifest.get("signature")
    ):
        raise ValueError("A completed, validated regional export is required for a county report")
    reports.mkdir(parents=True, exist_ok=True)
    maps = reports / "maps"
    maps.mkdir(exist_ok=True)
    name, slug = region["name"], region["slug"]
    # Windows filenames cannot contain reserved punctuation or trailing dots/spaces.
    filename = re.sub(r'[<>:"/\\|?*]', "-", name).strip(" .") + ".md"
    path = reports / filename
    county_root = output.parent.parent if output.parent.name == "runs" else output
    sites = pyogrio.read_dataframe(output / "results.gpkg", layer="sites")
    study = pyogrio.read_dataframe(output / "results.gpkg", layer="study_area")
    context = load_context(manifest, study, sites)
    presentation_signature = fingerprint(
        {
            "code": code_signature("reports.py", "cartography.py"),
            "context": context["metadata"],
            "analysis": manifest["signature"],
        }
    )
    preserve_previous_report(reports, path, slug, manifest["signature"], county_root, presentation_signature)
    overview, candidates_map = maps / f"{slug}-overview.png", maps / f"{slug}-candidates.png"
    scope_label = "Dorset Council area, excluding BCP" if region["authority_codes"] == ["E06000059"] else None
    plot_map(sites, study, overview, name, context=context, scope_label=scope_label)
    plot_map(
        sites, study, candidates_map, name, candidates_only=True, context=context, scope_label=scope_label
    )
    atomic_json(
        maps / f"{slug}-style.json",
        {
            "style": STYLE_VERSION,
            "presentation_signature": presentation_signature,
            "analysis_signature": manifest["signature"],
            "basemap": context["metadata"],
            "geometry_note": "Original analysis is unchanged; map shapes are display representations.",
        },
    )
    candidates = ordered_candidates(sites)
    minimum_area = manifest.get("parameters", {}).get("min_candidate_area_m2", 100)
    small_count = int(sites.geometry.area.lt(minimum_area).sum())
    uncertain_edge = sites.reasons.str.contains("regional_context_incomplete") & ~sites.status.isin(
        ["access_evidenced", "permissive_access_evidenced"]
    )
    counts = manifest["counts"]
    completed = now()
    sample = pd.read_csv(output / "review_sample.csv").fillna("")
    reviewed_ids = set(sample.loc[sample.review_outcome.ne(""), "site_id"])
    desk_review = pd.DataFrame()
    ledger = county_root / "desk_review.csv"
    review_snapshot = None
    if ledger.exists():
        import shapely

        desk_review = pd.read_csv(ledger).fillna("")
        geometry_hashes = dict(
            zip(sites.site_id, sites.geometry.map(lambda g: fingerprint(shapely.normalize(g).wkb_hex)))
        )
        desk_review = desk_review.loc[
            desk_review.site_id.map(geometry_hashes).eq(desk_review.geometry_sha256)
        ]
        version = fingerprint(desk_review.to_dict(orient="records"))
        review_snapshot = county_root / "reviews" / version / "desk_review.csv"
        if not review_snapshot.exists():
            review_snapshot.parent.mkdir(parents=True, exist_ok=True)
            desk_review.to_csv(review_snapshot, index=False)
        reviewed_ids.update(desk_review.site_id)
    reviewed = len(reviewed_ids)
    links = {
        key: relative_link(output / target, reports)
        for key, target in {
            "map": "map/index.html",
            "csv": "sites.csv",
            "candidates": "likely_access_islands.csv",
            "permissive": "permissive_access_evidenced.csv",
            "gis": "results.gpkg",
            "review": "review_sample.csv",
            "quality": "quality_report.md",
            "progress": "progress.json",
        }.items()
    }
    lines = [
        f"# {name} — access-land research report",
        "",
        f"Generated: {completed}",
        "",
        f"Processing and automated export validation are complete. These are screening findings. Recorded review outcomes: {reviewed}; blank reviews remain outstanding.",
        "",
        *(
            ["**Synthetic demonstration — not real access islands.**", ""]
            if manifest["scope"].get("synthetic")
            else []
        ),
        f"Study authorities: {', '.join(region['authority_codes']) or 'custom boundary'}.",
        f"Surrounding context buffer requested: {region['buffer_m'] / 1000:g} km. "
        f"Limited by source extract coverage: {'yes' if region['context_limited_by_source'] else 'no'}.",
        "",
        "## Results",
        "",
        "| Classification | Sites |",
        "| --- | ---: |",
    ]
    lines.extend(f"| {status.replace('_', ' ')} | {counts.get(status, 0)} |" for status in COLOURS)
    lines.extend(
        [
            f"| Total | {len(sites)} |",
            "",
            f"Access-land area in reported complete sites: {sites.area_ha.sum():,.2f} ha.",
            f"Sites containing registered common land: {manifest.get('registered_common_sites', 'not recorded')}. "
            f"Sites containing section 15 statutory land: {manifest.get('section15_sites', 'not recorded')}. "
            f"Unverified OSM common areas in the surrounding context: {manifest.get('unverified_osm_commons', 0)}.",
            f"Sites with unresolved connections at the regional context edge: {int(uncertain_edge.sum())}.",
            "",
            f"Very small polygons below the {minimum_area:g} m² geometry-review threshold: {small_count} "
            f"of {len(sites)}. Sites at or above that threshold: {len(sites) - small_count}.",
            "Tiny fragments may arise from coastline clipping or source boundaries. They remain in the full inventory; "
            "an absence of mapped approaches alone does not promote them to candidates. Grey dots show uncertain sites with this geometry flag.",
            "",
            "Sites crossing the study boundary retain their full geometry and stable IDs. They may also appear in neighbouring reports.",
            "",
            "## Maps",
            "",
            f"![{name}: all access-land sites](maps/{overview.name})",
            "",
            f"![{name}: screening candidates](maps/{candidates_map.name})",
            "",
            "Map dots keep small sites visible; coloured polygons show their mapped extent. Candidate numbers correspond to the table below.",
            "",
            "The atlas graphics include close-up panels for the largest candidates. "
            + (
                "Roads, woodland, water and settlement names come from the cached OSM extract. Basemap paths are context and do not establish access rights."
                if context["metadata"].get("available")
                else "Detailed OSM context was unavailable locally; these graphics use the study outline."
            ),
            "",
            f"Vector graphics for sharing or printing: [overview SVG](maps/{overview.stem}.svg) · [candidate SVG](maps/{candidates_map.stem}.svg).",
            "",
            f"[Open the interactive map]({links['map']}) to search all sites, inspect boundaries and follow site links.",
            "",
            "## Candidate sites",
            "",
        ]
    )
    if len(candidates):
        lines.extend(
            [
                "Up to 25 candidates are listed below, largest first; the candidate CSV contains the complete list.",
                "",
                "| Map number | Site | Area (ha) | Latitude | Longitude | Council |",
                "| ---: | --- | ---: | ---: | ---: | --- |",
            ]
        )
        for number, row in enumerate(candidates.head(25).itertuples(), 1):
            url = links["map"] + "?site=" + row.site_id
            council = str(row.council).replace("|", "\\|")
            lines.append(
                f"| {number} | [{row.site_id}]({url}) | {row.area_ha:.2f} | {row.latitude:.6f} | {row.longitude:.6f} | {council} |"
            )
    else:
        lines.append(
            "No likely access islands were identified in this screening run. Uncertain sites still require review."
        )
    if len(desk_review):
        lines.extend(
            [
                "",
                "## Desktop review",
                "",
                "These checks compare published records and mapped evidence. They do not establish current physical access or replace the legal definitive map. "
                "Review records are versioned separately from the immutable analysis package.",
                "",
                f"[Complete dated desk-review records]({relative_link(review_snapshot, reports)}).",
                "",
                "| Site | Review group | Date | Outcome |",
                "| --- | --- | --- | --- |",
            ]
        )
        for row in desk_review.itertuples():
            lines.append(
                f"| [{row.site_id}]({links['map']}?site={row.site_id}) | {row.review_group} | {row.review_date} | {str(row.review_outcome).replace('|', ';')} |"
            )
        lines.append("")
    lines.extend(
        [
            "",
            "## Downloads and review",
            "",
            f"- [Complete site CSV]({links['csv']})",
            f"- [Candidate CSV]({links['candidates']})",
            f"- [Permissive access evidenced CSV]({links['permissive']})",
            f"- [GIS polygons and route evidence]({links['gis']})",
            f"- [Review sample]({links['review']})",
            f"- [Quality and provenance report]({links['quality']})",
            f"- [Run progress]({links['progress']})",
            "",
            "## Coverage and interpretation",
            "",
            "Public access does not imply public ownership. Missing mapped approaches do not prove that a lawful entrance is absent. "
            "Road anchors are inferred from OSM; private, permissive and unknown routes, barriers, bridges and matching tolerance affect the result.",
            "Permissive access evidenced means the permitted graph reaches a road at exact, default and relaxed tolerances using strong connections. "
            "It does not rule out an unmapped public approach, and permission may change. Public access evidenced takes precedence.",
            "",
            "Section 15 land is included as additional statutory study land, retaining the recorded Act and common name in legal_source_attributes. "
            "Overlapping statutory polygons are united so area is counted once. OSM common tags are an optional, unverified context layer; "
            "they cannot establish statutory rights or positive access. Internal fences, terrain and current entrances still require review.",
            "",
            "",
        ]
    )
    gaps = manifest.get("coverage", {}).get("missing_authorities", [])
    lines.append(
        "Missing official PRoW coverage in the processing area: "
        + (", ".join(r["name"] for r in gaps) or "none reported")
        + "."
    )
    if "E06000046" in region["authority_codes"]:
        lines.extend(
            [
                "",
                "For local review, use the council's [rights-of-way maps](https://www.iow.gov.uk/article/2291/Rights-of-way-maps) "
                "and [routes, network maps and closures](https://www.iow.gov.uk/article/2276/Routes-and-paths). "
                "The council dates its published definitive-map scans to 29 February 2000 and notes subsequent changes. "
                "Its leisure map gives approximate routes and is not a substitute for definitive records. "
                "These resources have not yet been used to verify individual candidates in this run.",
            ]
        )
    lines.extend(
        ["", "## Source snapshots", "", "| Source | Snapshot/version | Retrieved |", "| --- | --- | --- |"]
    )
    for role, source in manifest["sources"].items():
        version = source_version(source)
        title = f"[{role}]({source['url']})" if source.get("url") else role
        lines.append(f"| {title} | {version} | {source.get('retrieved', 'local input')} |")
    lines.extend(
        [
            "",
            "Publication, catalogue modification and retrieval dates do not establish when individual rights or entrances were last checked. "
            "Section 15 records retain entered/version fields and the Act in the full CSV/GIS provenance.",
        ]
    )
    atomic_text(path, "\n".join(lines) + "\n")
    with FileLock(reports / "index.lock", timeout=60):
        update_index(
            reports,
            slug,
            name,
            filename,
            completed,
            sites,
            counts,
            region,
            manifest,
            small_count,
            presentation_signature,
        )
    return path


def update_index(
    reports,
    slug,
    name,
    filename,
    completed,
    sites,
    counts,
    region,
    manifest,
    small_count,
    presentation_signature=None,
):
    registry = read_json(reports / "index.json", {})
    registry[slug] = {
        "name": name,
        "report": filename,
        "completed": completed,
        "sites": len(sites),
        "counts": counts,
        "very_small_polygons": small_count,
        "scope": region,
        "analysis_signature": manifest["signature"],
        "presentation_signature": presentation_signature,
    }
    atomic_json(reports / "index.json", registry)
    index = [
        "# Completed regional reports",
        "",
        "Reports are generated after automated processing and export validation. Manual review is recorded separately.",
        "",
        "| Area | Sites | Candidates | Permissive access evidenced | Uncertain | Very small polygons | Completed |",
        "| --- | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for record in sorted(registry.values(), key=lambda r: r["name"]):
        index.append(
            f"| [{record['name']}]({quote(record['report'])}) | {record['sites']} | "
            f"{record['counts'].get('likely_access_island', 0)} | {record['counts'].get('permissive_access_evidenced', 0)} | {record['counts'].get('uncertain', 0)} | "
            f"{record.get('very_small_polygons', 'unknown')} | {record['completed'][:10]} |"
        )
    atomic_text(reports / "README.md", "\n".join(index) + "\n")
