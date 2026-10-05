"""Small synthetic input set to exercise the entire pipeline without national downloads."""

from __future__ import annotations

from pathlib import Path

import geopandas as gpd
import osmium
from pyproj import Transformer
from shapely.geometry import LineString, box

from .common import CRS, atomic_json, digest, now, write_layer


def make_demo(root: Path):
    raw = root / "demo_inputs"
    raw.mkdir(parents=True, exist_ok=True)
    x, y = 450000, 100000
    squares = [box(x + offset, y, x + offset + 100, y + 100) for offset in [0, 500, 1000, 1500]]
    records = [
        {"id": i, "name": name, "geometry": geom}
        for i, (name, geom) in enumerate(
            zip(
                [
                    "Synthetic: road-connected",
                    "Synthetic: private track only",
                    "Synthetic: permissive approach",
                    "Synthetic: disconnected public path",
                ],
                squares,
            )
        )
    ]
    write_layer(gpd.GeoDataFrame(records, crs=CRS), raw / "land.gpkg", "land")
    # A duplicate in the dedicated layer exercises overlap reconciliation.
    write_layer(gpd.GeoDataFrame([records[0]], crs=CRS), raw / "dedicated.gpkg", "land")
    line = LineString([(x + 10, y + 10), (x + 90, y + 10)])
    write_layer(gpd.GeoDataFrame([{"id": 1, "geometry": line}], crs=CRS), raw / "prow.gpkg", "routes")
    boundary = box(x - 2000, y - 2000, x + 4000, y + 2000)
    for role, code, name in [
        ("countries", "E92000001", "England"),
        ("counties", "E06000046", "Synthetic county"),
        ("councils", "E06000046", "Synthetic council"),
    ]:
        write_layer(
            gpd.GeoDataFrame([{"code": code, "name": name, "geometry": boundary}], crs=CRS),
            raw / f"{role}.gpkg",
            role,
        )
    pbf = raw / "demo.osm.pbf"
    pbf.unlink(missing_ok=True)
    transform = Transformer.from_crs(CRS, 4326, always_xy=True).transform
    with osmium.SimpleWriter(str(pbf)) as writer:
        ways = [
            ([(x - 100, y - 100), (x + 2000, y - 100)], {"highway": "residential"}),
            ([(x + 50, y - 100), (x + 50, y + 50)], {"highway": "footway", "foot": "yes"}),
            ([(x + 550, y - 100), (x + 550, y + 50)], {"highway": "track", "access": "private"}),
            ([(x + 1050, y - 100), (x + 1050, y + 50)], {"highway": "path", "foot": "permissive"}),
            ([(x + 1510, y + 50), (x + 1590, y + 50)], {"highway": "path", "designation": "public_footpath"}),
        ]
        # Add all junction coordinates to the road so node IDs, not geometric crossings, connect OSM ways.
        ways[0] = (
            [
                (x - 100, y - 100),
                (x + 50, y - 100),
                (x + 550, y - 100),
                (x + 1050, y - 100),
                (x + 2000, y - 100),
            ],
            ways[0][1],
        )
        nodes = {}
        for coords, _ in ways:
            for xy in coords:
                if xy not in nodes:
                    nodes[xy] = len(nodes) + 1
        for xy, node_id in nodes.items():
            lon, lat = transform(*xy)
            writer.add_node(osmium.osm.mutable.Node(id=node_id, location=osmium.osm.Location(lon, lat)))
        for way_id, (coords, tags) in enumerate(ways, 1):
            writer.add_way(osmium.osm.mutable.Way(id=way_id, nodes=[nodes[xy] for xy in coords], tags=tags))
    sources = {}
    for role in ["land", "dedicated", "prow", "countries", "counties", "councils"]:
        p = raw / f"{role}.gpkg"
        sources[role] = {
            "path": str(p),
            "id_field": "id",
            "code_field": "code",
            "name_field": "name",
            "sha256": digest(p),
            "retrieved": now(),
            "local": True,
            "version": "synthetic",
        }
    sources["osm"] = {"path": str(pbf), "sha256": digest(pbf), "retrieved": now(), "version": "synthetic"}
    atomic_json(root / "sources.json", sources)
    atomic_json(root / "coverage.json", {"missing_authorities": [], "version": "synthetic"})
    return {"synthetic": True, "analysis": {"tile_size_m": 1000}}
