"""Offline atlas graphics. Basemap features are context, never access evidence."""

from __future__ import annotations

import logging
import math
import os
from pathlib import Path

import geopandas as gpd
import matplotlib
import pyogrio
import shapely
from filelock import FileLock

matplotlib.use("Agg")
import matplotlib.patheffects as effects
import matplotlib.pyplot as plt
from matplotlib import font_manager
from matplotlib.lines import Line2D
from matplotlib.patches import Rectangle

from .common import CRS, atomic_json, digest, empty_frame, fingerprint, read_json, write_layer

LOG = logging.getLogger(__name__)
EXTRACT_VERSION = 1
STYLE_VERSION = "atlas-1"
PAPER = "#f7f5ef"
INK = "#173f3c"
MUTED = "#627873"
SEA = "#e2edf0"
LAND = "#f5f3eb"
CORAL = "#ef4b36"
COLOURS = {
    "likely_access_island": CORAL,
    "uncertain": "#c39b3b",
    "access_evidenced": "#208571",
    "permissive_access_evidenced": "#527fc1",
}
LABELS = {
    "likely_access_island": "Possible access islands",
    "access_evidenced": "Public access evidenced",
    "permissive_access_evidenced": "Permissive access evidenced",
    "uncertain": "Uncertain / needs review",
}
LAYERS = ("areas", "lines", "places")


def area_kind(tags):
    if tags.get("natural") == "water" or tags.get("landuse") in {"reservoir", "basin"}:
        return "water"
    if tags.get("natural") == "wood" or tags.get("landuse") == "forest":
        return "woodland"
    if tags.get("natural") in {"heath", "scrub", "grassland"}:
        return "open_land"
    if tags.get("landuse") in {"residential", "industrial", "commercial", "retail"}:
        return "settlement"
    return None


def line_kind(tags):
    highway = tags.get("highway", "")
    if highway in {"motorway", "trunk", "primary", "secondary"} or highway.endswith("_link"):
        return "major_road"
    if highway in {"tertiary", "unclassified", "residential", "living_street", "service"}:
        return "minor_road"
    if highway in {"path", "footway", "bridleway", "track", "steps", "cycleway"}:
        return "path"
    if tags.get("waterway") in {"river", "stream", "canal"}:
        return "river" if tags["waterway"] != "stream" else "stream"
    if tags.get("railway") == "rail":
        return "railway"
    return None


def extract_basemap(pbf, directory):
    """Cache a lightweight display inventory separately from route preparation."""
    import osmium
    from pyproj import Transformer
    from shapely.ops import transform

    signature = fingerprint({"pbf": digest(pbf), "extract_version": EXTRACT_VERSION})
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "context.gpkg"
    metadata = directory / "context.json"
    with FileLock(directory / "context.lock", timeout=120):
        previous = read_json(metadata, {})
        if previous.get("signature") == signature and path.exists():
            return path, previous
        LOG.info("Preparing offline cartographic context from %s", pbf.name)
        factory = osmium.geom.WKBFactory()
        project = Transformer.from_crs(4326, CRS, always_xy=True).transform
        records = {key: [] for key in LAYERS}
        rejected = {key: 0 for key in LAYERS}

        class Features(osmium.SimpleHandler):
            def node(self, node):
                if node.tags.get("place") not in {"city", "town", "village"} or not node.tags.get("name"):
                    return
                try:
                    x, y = project(node.location.lon, node.location.lat)
                    records["places"].append(
                        {
                            "kind": node.tags["place"],
                            "name": node.tags["name"],
                            "geometry": shapely.Point(x, y),
                        }
                    )
                except (RuntimeError, ValueError):
                    rejected["places"] += 1

            def way(self, way):
                kind = line_kind(way.tags)
                if kind is None:
                    return
                try:
                    geom = transform(project, shapely.from_wkb(factory.create_linestring(way)))
                    if geom.is_empty or geom.length == 0:
                        return
                    records["lines"].append(
                        {"kind": kind, "name": way.tags.get("name", ""), "geometry": geom}
                    )
                except (RuntimeError, ValueError):
                    rejected["lines"] += 1

            def area(self, area):
                kind = area_kind(area.tags)
                if kind is None:
                    return
                try:
                    geom = shapely.make_valid(
                        transform(project, shapely.from_wkb(factory.create_multipolygon(area)))
                    )
                    # Geometry collections can contain lines after repair: retain only polygons.
                    if geom.geom_type == "GeometryCollection":
                        geom = shapely.union_all(
                            [g for g in geom.geoms if g.geom_type in {"Polygon", "MultiPolygon"}]
                        )
                    if geom.is_empty or geom.area == 0:
                        return
                    records["areas"].append(
                        {"kind": kind, "name": area.tags.get("name", ""), "geometry": geom}
                    )
                except (RuntimeError, ValueError):
                    rejected["areas"] += 1

        Features().apply_file(str(pbf), locations=True, idx="flex_mem")
        temporary = path.with_suffix(".tmp.gpkg")
        temporary.unlink(missing_ok=True)
        for layer, rows in records.items():
            frame = gpd.GeoDataFrame(rows, crs=CRS) if rows else empty_frame(["kind", "name"])
            write_layer(frame, temporary, layer)
        os.replace(temporary, path)
        value = {
            "signature": signature,
            "source": str(pbf.resolve()),
            "source_sha256": digest(pbf),
            "extract_version": EXTRACT_VERSION,
            "counts": {key: len(rows) for key, rows in records.items()},
            "rejected": rejected,
            "attribution": "© OpenStreetMap contributors; ODbL 1.0",
            "purpose": "Display context only. Paths and land cover do not establish access rights.",
        }
        atomic_json(metadata, value)
        return path, value


def load_context(manifest, study, sites):
    """Use only existing local sources; reports never download map tiles."""
    result = {layer: empty_frame(["kind", "name"]) for layer in LAYERS}
    result["land"] = study.copy()
    result["metadata"] = {"available": False, "reason": "No cached regional OSM extract"}
    sources = manifest.get("sources", {})
    source = sources.get("osm", {})
    pbf = Path(source.get("path") or "__missing__")
    # Avoid introducing a whole-GB scan for a report. Named regions already cache county extracts.
    if pbf.is_file() and pbf.name.endswith(".pbf") and pbf.stat().st_size < 500_000_000:
        context_path, metadata = extract_basemap(pbf, pbf.parent / "cartography")
        result["metadata"] = {**metadata, "available": True, "snapshot": source.get("osm_snapshot", "")}
        for layer in LAYERS:
            result[layer] = pyogrio.read_dataframe(context_path, layer=layer)
    else:
        LOG.info("No county PBF available: using the geographic outline without detailed context")
    countries = sources.get("countries", {})
    country_path = Path(countries.get("path") or "__missing__")
    if country_path.is_file():
        # The country source supplies a real coastline outside the study boundary too.
        country = pyogrio.read_dataframe(country_path, layer=countries.get("layer")).to_crs(CRS)
        country.geometry = country.geometry.simplify(15)
        bounds = study.total_bounds
        if len(sites):
            bounds = gpd.GeoSeries(
                [study.geometry.union_all(), sites.geometry.union_all()], crs=CRS
            ).total_bounds
        margin = max(bounds[2] - bounds[0], bounds[3] - bounds[1], 100) * 0.65
        window = shapely.box(bounds[0] - margin, bounds[1] - margin, bounds[2] + margin, bounds[3] + margin)
        result["land"] = gpd.GeoDataFrame(geometry=country.geometry.intersection(window), crs=CRS)
    return result


def ordered_candidates(sites):
    return sites.loc[sites.status.eq("likely_access_island")].sort_values(
        ["area_ha", "site_id"], ascending=[False, True]
    )


def viewport(bounds, width, height, padding=0.08):
    west, south, east, north = bounds
    cx, cy = (west + east) / 2, (south + north) / 2
    dx, dy = max(east - west, 100), max(north - south, 100)
    dx, dy = dx * (1 + 2 * padding), dy * (1 + 2 * padding)
    if dx / dy < width / height:
        dx = dy * width / height
    else:
        dy = dx * height / width
    return cx - dx / 2, cy - dy / 2, cx + dx / 2, cy + dy / 2


def in_view(frame, bounds):
    if frame.empty:
        return frame
    return frame.iloc[frame.sindex.query(shapely.box(*bounds), predicate="intersects")]


def display_tolerance(ax, bounds):
    """A fraction of an output pixel: reduce SVG size without changing source data."""
    pixels = ax.get_position().width * ax.figure.get_size_inches()[0] * 200
    return (bounds[2] - bounds[0]) / pixels * 0.35


def draw_context(ax, context, study, bounds, detail=False):
    ax.set_facecolor(SEA)
    ax.set_aspect("equal")
    ax.set_xlim(bounds[0], bounds[2])
    ax.set_ylim(bounds[1], bounds[3])
    # Retain the background patch while removing every chart convention.
    ax.set_xticks([])
    ax.set_yticks([])
    for spine in ax.spines.values():
        spine.set_visible(False)
    land = in_view(context["land"], bounds)
    if len(land):
        land.plot(ax=ax, facecolor=LAND, edgecolor="#a4bbb5", linewidth=0.65, zorder=1)
    tolerance = display_tolerance(ax, bounds)
    areas = in_view(context["areas"], bounds).copy()
    areas.geometry = areas.geometry.simplify(tolerance)
    colours = {"settlement": "#e5e1d7", "open_land": "#e6ebd9", "woodland": "#d3e1ce", "water": "#cfE3e8"}
    for kind, colour in colours.items():
        subset = areas.loc[areas.kind.eq(kind)]
        if not detail:
            subset = subset.loc[subset.geometry.area.ge(3000)]
        if len(subset):
            subset.plot(ax=ax, facecolor=colour, edgecolor="none", zorder=2)
    lines = in_view(context["lines"], bounds).copy()
    lines.geometry = lines.geometry.simplify(tolerance)
    for kind, colour, width in [
        ("river", "#afcdd3", 0.7),
        ("stream", "#bfd7d9", 0.4),
        ("railway", "#b9c0b7", 0.45),
    ]:
        if kind == "stream" and not detail:
            continue
        subset = lines.loc[lines.kind.eq(kind)]
        if len(subset):
            subset.plot(ax=ax, color=colour, linewidth=width, zorder=3)
    minor = lines.loc[lines.kind.eq("minor_road")]
    if len(minor):
        minor.plot(ax=ax, color="#c9c9b9", linewidth=0.55 if detail else 0.32, zorder=4)
    major = lines.loc[lines.kind.eq("major_road")]
    if len(major):
        major.plot(ax=ax, color="#ffffff", linewidth=2 if detail else 1.5, zorder=5)
        major.plot(ax=ax, color="#c8bfa8", linewidth=0.8 if detail else 0.55, zorder=6)
    if detail:
        paths = lines.loc[lines.kind.eq("path")]
        if len(paths):
            paths.plot(ax=ax, color="#b1b7a8", linewidth=0.55, linestyle=(0, (2, 2)), zorder=6)
    elif len(study):
        # Surrounding geography is useful context; fade it to distinguish the study area.
        boundary = study.geometry.simplify(tolerance)
        outside = shapely.box(*bounds).difference(boundary.union_all())
        gpd.GeoSeries([outside], crs=CRS).plot(ax=ax, color=PAPER, alpha=0.42, edgecolor="none", zorder=7)
        boundary.boundary.plot(ax=ax, color="#75948b", linewidth=0.8, linestyle=(0, (3, 3)), zorder=8)
    ax.set_xlim(bounds[0], bounds[2])
    ax.set_ylim(bounds[1], bounds[3])


def plot_sites(ax, sites, candidates_only=False, detail=False):
    if sites.empty:
        return
    sites.plot(ax=ax, facecolor="#b7c8b0", edgecolor="#8ba890", linewidth=0.35, alpha=0.45, zorder=9)
    shown = sites.loc[sites.status.eq("likely_access_island")] if candidates_only else sites
    for status in ["uncertain", "access_evidenced", "permissive_access_evidenced", "likely_access_island"]:
        subset = shown.loc[shown.status.eq(status)]
        tiny = subset.reasons.fillna("").str.contains("very_small_site_geometry")
        if status == "uncertain":
            subset = subset.loc[~tiny]
        if subset.empty:
            continue
        colour = COLOURS[status]
        highlighted = status == "likely_access_island"
        subset.plot(
            ax=ax,
            facecolor=colour,
            edgecolor=colour,
            linewidth=1.4 if highlighted else 0.65,
            alpha=0.82 if highlighted else 0.6,
            zorder=13 if highlighted else 10,
            path_effects=[effects.Stroke(linewidth=3.2, foreground="white"), effects.Normal()]
            if highlighted
            else [],
        )
        if not detail:
            points = subset.geometry.representative_point()
            if highlighted:
                points.plot(ax=ax, color=colour, markersize=115, alpha=0.13, linewidth=0, zorder=12)
            points.plot(
                ax=ax,
                color=colour,
                edgecolor="white",
                linewidth=0.8 if highlighted else 0.45,
                markersize=29 if highlighted else 9,
                alpha=1 if highlighted else 0.85,
                zorder=14 if highlighted else 11,
            )
    if not candidates_only and not detail:
        small = sites.loc[
            sites.status.eq("uncertain") & sites.reasons.fillna("").str.contains("very_small_site_geometry")
        ]
        if len(small):
            small.geometry.representative_point().plot(
                ax=ax, color="#899c96", markersize=2.5, alpha=0.45, linewidth=0, zorder=10
            )


def place_labels(ax, context, bounds, candidates, detail=False):
    places = in_view(context["places"], bounds).copy()
    if places.empty:
        return []
    places["rank"] = places.kind.map({"city": 0, "town": 1, "village": 2})
    if not detail and bounds[2] - bounds[0] > 65000:
        places = places.loc[places["rank"].lt(2)]
    points = candidates.geometry.representative_point() if len(candidates) else []
    ax.figure.canvas.draw()
    renderer = ax.figure.canvas.get_renderer()
    boxes, labels = [], []
    for row in places.sort_values(["rank", "name"]).itertuples():
        point = row.geometry
        # Reserve space for candidate markers, which are the focus of the graphic.
        if not detail and any(point.distance(p) < (bounds[2] - bounds[0]) * 0.012 for p in points):
            continue
        label = ax.annotate(
            row.name,
            (point.x, point.y),
            xytext=(4, 3),
            textcoords="offset points",
            fontsize=8 if detail else 9,
            color=MUTED,
            fontweight="semibold" if row.kind != "village" else "normal",
            zorder=15,
            path_effects=[effects.Stroke(linewidth=2.5, foreground=PAPER), effects.Normal()],
            clip_on=True,
        )
        bbox = label.get_window_extent(renderer).expanded(1.12, 1.3)
        if (
            any(bbox.overlaps(b) for b in boxes)
            or not ax.get_window_extent(renderer).contains(bbox.x0, bbox.y0)
            or not ax.get_window_extent(renderer).contains(bbox.x1, bbox.y1)
        ):
            label.remove()
            continue
        boxes.append(bbox)
        labels.append(label)
        if len(labels) >= (5 if detail else 14):
            break
    return boxes


def candidate_labels(ax, candidates, occupied):
    renderer = ax.figure.canvas.get_renderer()
    offsets = [
        (11, 11),
        (-11, 11),
        (11, -11),
        (-11, -11),
        (22, 0),
        (-22, 0),
        (0, 23),
        (0, -23),
        (30, 18),
        (-30, -18),
        (40, 0),
        (-40, 0),
    ]
    for number, row in enumerate(candidates.head(25).itertuples(), 1):
        point = row.geometry.representative_point()
        for index, offset in enumerate(offsets):
            annotation = ax.annotate(
                str(number),
                (point.x, point.y),
                xytext=offset,
                textcoords="offset points",
                ha="center",
                va="center",
                fontsize=8.5,
                fontweight="bold",
                color="white",
                zorder=20,
                bbox={
                    "boxstyle": "circle,pad=0.28",
                    "facecolor": CORAL,
                    "edgecolor": PAPER,
                    "linewidth": 1.4,
                },
                arrowprops={"arrowstyle": "-", "color": CORAL, "linewidth": 0.65},
            )
            annotation.update_positions(renderer)
            annotation.update_bbox_position_size(renderer)
            bbox = annotation.get_bbox_patch().get_window_extent(renderer).expanded(1.15, 1.15)
            if not any(bbox.overlaps(b) for b in occupied) or index == len(offsets) - 1:
                occupied.append(bbox)
                break
            annotation.remove()


def scale_bar(ax, bounds, detail=False):
    target = (bounds[2] - bounds[0]) * (0.24 if detail else 0.16)
    power = 10 ** math.floor(math.log10(target))
    length = max(n * power for n in [0.1, 0.2, 0.5, 1, 2, 5, 10] if n * power <= target)
    fraction = length / (bounds[2] - bounds[0])
    x, y = 0.04, 0.06
    label = f"{length / 1000:g} km" if length >= 1000 else f"{length:g} m"
    ax.plot([x, x + fraction], [y, y], transform=ax.transAxes, color=PAPER, linewidth=5, zorder=24)
    ax.plot([x, x + fraction], [y, y], transform=ax.transAxes, color=INK, linewidth=1.7, zorder=25)
    for edge in [x, x + fraction]:
        ax.plot(
            [edge, edge], [y - 0.009, y + 0.009], transform=ax.transAxes, color=INK, linewidth=1.1, zorder=25
        )
    ax.text(
        x,
        y + 0.017,
        label,
        transform=ax.transAxes,
        fontsize=7 if detail else 9,
        color=INK,
        zorder=25,
        path_effects=[effects.Stroke(linewidth=3, foreground=PAPER), effects.Normal()],
    )


def inset_maps(fig, candidates, sites, context, study):
    fig.text(0.775, 0.76, "A CLOSER LOOK", fontsize=10, fontweight="bold", color=INK)
    count = min(3, len(candidates))
    if not count:
        fig.text(
            0.775,
            0.70,
            "No candidates identified\nin this screening run.",
            fontsize=12,
            color=MUTED,
            linespacing=1.8,
        )
        return
    height = 0.137 if count > 1 else 0.32
    for index, row in enumerate(candidates.head(count).itertuples()):
        top = 0.71 - index * 0.184
        fig.text(
            0.775,
            top + 0.014,
            f"{index + 1:02d}  /  {row.area_ha:,.2f} ha",
            color=CORAL,
            fontsize=11,
            fontweight="bold",
        )
        ax = fig.add_axes([0.775, top - height, 0.185, height])
        west, south, east, north = row.geometry.bounds
        margin = max(east - west, north - south, 300) * 0.32 + 160
        bounds = viewport(
            (west - margin, south - margin, east + margin, north + margin), 14 * 0.185, 10 * height, padding=0
        )
        draw_context(ax, context, study, bounds, detail=True)
        plot_sites(ax, in_view(sites, bounds), candidates_only=True, detail=True)
        place_labels(ax, context, bounds, candidates.iloc[0:0], detail=True)
        scale_bar(ax, bounds, detail=True)
        # GeoPandas supplies CRS axis labels on every plot call. Remove them last.
        ax.set_xlabel("")
        ax.set_ylabel("")
        for spine in ax.spines.values():
            spine.set_visible(True)
            spine.set_color("#d6ded4")
            spine.set_linewidth(0.7)
        if count == 1:
            fig.text(
                0.775,
                0.33,
                f"{row.latitude:.5f}° N\n{abs(row.longitude):.5f}° {'W' if row.longitude < 0 else 'E'}",
                fontsize=11,
                color=MUTED,
                linespacing=1.7,
            )
            fig.text(
                0.775,
                0.245,
                "Numbers match the candidate\ntable in the county report.",
                fontsize=9,
                color=MUTED,
                linespacing=1.6,
            )


def plot_map(sites, study, path, name, candidates_only=False, context=None, scope_label=None):
    """Render publication-sized PNG and SVG maps without axes, grids or tile services."""
    if context is None:
        context = {layer: empty_frame(["kind", "name"]) for layer in LAYERS}
        context.update(land=study, metadata={"available": False})
    family = (
        "Segoe UI" if any(f.name == "Segoe UI" for f in font_manager.fontManager.ttflist) else "DejaVu Sans"
    )
    with plt.rc_context({"font.family": family, "svg.fonttype": "path"}):
        fig = plt.figure(figsize=(14, 10), facecolor=PAPER)
        try:
            candidates = ordered_candidates(sites)
            fig.text(0.04, 0.945, "ACCESS ISLANDS  /  ENGLAND", fontsize=11, color=INK, fontweight="bold")
            fig.text(0.04, 0.865, name, fontsize=43, fontweight="bold", color=INK)
            subtitle = (
                "Possible access islands · screening candidates"
                if candidates_only
                else "Statutory access land · county overview"
            )
            if scope_label:
                subtitle = (
                    ("Possible access islands" if candidates_only else "Statutory access land")
                    + " · "
                    + scope_label
                )
            fig.text(0.042, 0.822, subtitle, fontsize=14, color=MUTED)
            for x, value, title, colour in [
                (0.745, len(candidates), "POSSIBLE ISLANDS", CORAL),
                (0.87, len(sites), "MAPPED SITES", INK),
            ]:
                fig.text(x, 0.88, f"{value:,}", fontsize=33, fontweight="bold", color=colour)
                fig.text(x, 0.849, title, fontsize=8.5, color=MUTED, fontweight="bold")
            fig.add_artist(
                Line2D([0.04, 0.96], [0.793, 0.793], transform=fig.transFigure, color="#d5ded3", linewidth=1)
            )
            rect = [0.04, 0.21, 0.695 if candidates_only else 0.92, 0.56]
            ax = fig.add_axes(rect)
            geometry = study.geometry.union_all()
            if len(sites):
                geometry = shapely.union_all([geometry, sites.geometry.union_all()])
            bounds = viewport(geometry.bounds, rect[2] * 14, rect[3] * 10, padding=0.075)
            draw_context(ax, context, study, bounds)
            plot_sites(ax, sites, candidates_only)
            boxes = place_labels(ax, context, bounds, candidates)
            if candidates_only:
                candidate_labels(ax, candidates, boxes)
                inset_maps(fig, candidates, sites, context, study)
            scale_bar(ax, bounds)
            ax.set_xlabel("")
            ax.set_ylabel("")
            ax.annotate(
                "",
                (0.96, 0.94),
                xytext=(0.96, 0.87),
                xycoords="axes fraction",
                textcoords="axes fraction",
                arrowprops={"arrowstyle": "-|>", "color": INK, "linewidth": 1},
                zorder=25,
            )
            ax.text(
                0.96,
                0.955,
                "N",
                transform=ax.transAxes,
                ha="center",
                fontsize=10,
                fontweight="bold",
                color=INK,
                zorder=25,
            )
            fig.add_artist(
                Line2D([0.04, 0.96], [0.182, 0.182], transform=fig.transFigure, color="#d5ded3", linewidth=1)
            )
            if candidates_only:
                entries = [("Possible access islands", CORAL), ("Other statutory access land", "#9ab396")]
            else:
                entries = [
                    (LABELS[s], COLOURS[s])
                    for s in [
                        "likely_access_island",
                        "access_evidenced",
                        "permissive_access_evidenced",
                        "uncertain",
                    ]
                ]
            for index, (label, colour) in enumerate(entries):
                x = 0.046 + index * (0.37 if candidates_only else 0.235)
                fig.add_artist(
                    Rectangle(
                        (x, 0.142),
                        0.009,
                        0.012,
                        transform=fig.transFigure,
                        facecolor=colour,
                        edgecolor="none",
                    )
                )
                fig.text(x + 0.015, 0.142, label, fontsize=10.5, color=INK)
            fig.text(
                0.045,
                0.11,
                "Circles keep small parcels visible; polygons show mapped extent. Findings require local verification.",
                fontsize=9,
                color=MUTED,
            )
            if context["metadata"].get("available"):
                note = "Offline OSM basemap · roads, woodland, water & settlements. Basemap paths do not establish access rights."
            else:
                note = "Outline map · detailed OSM context was unavailable in the local source cache."
            fig.text(0.045, 0.088, note, fontsize=8.5, color=MUTED)
            fig.text(
                0.045,
                0.065,
                "Access land: © Natural England. Contains OS data © Crown copyright and database right.",
                fontsize=8.5,
                color=MUTED,
            )
            fig.text(
                0.045,
                0.045,
                "Boundaries: ONS  ·  © OpenStreetMap contributors (ODbL)",
                fontsize=8.5,
                color=MUTED,
            )
            fig.text(
                0.96, 0.045, "ACCESS-LAND RESEARCH", ha="right", fontsize=8.5, color=INK, fontweight="bold"
            )
            path.parent.mkdir(parents=True, exist_ok=True)
            for suffix in [".png", ".svg"]:
                target = path.with_suffix(suffix)
                temporary = target.with_suffix(".tmp" + suffix)
                fig.savefig(
                    temporary,
                    dpi=200,
                    facecolor=PAPER,
                    metadata={"Title": f"{name} — {subtitle}"} if suffix == ".svg" else None,
                )
                os.replace(temporary, target)
        finally:
            plt.close(fig)
