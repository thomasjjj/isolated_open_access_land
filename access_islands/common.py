from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import geopandas as gpd

CRS = "EPSG:27700"


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(4 * 1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def source_digest(path: Path) -> str:
    if path.is_file():
        return digest(path)
    return fingerprint({str(p.relative_to(path)): digest(p) for p in sorted(path.rglob("*")) if p.is_file()})


def fingerprint(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def code_signature(*names) -> str:
    paths = [Path(__file__).parent / name for name in names] if names else sorted(Path(__file__).parent.glob("*.py"))
    return fingerprint({p.name: digest(p) for p in paths})


def atomic_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def read_json(path: Path, default=None):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def empty_frame(columns=(), crs=CRS):
    return gpd.GeoDataFrame({c: [] for c in columns}, geometry=[], crs=crs)


def write_layer(frame: gpd.GeoDataFrame, path: Path, layer: str):
    """Create valid empty layers too; never silently omit an empty result."""
    frame.to_file(path, layer=layer, driver="GPKG", engine="pyogrio", index=False, geometry_type="Unknown")


class UnionFind:
    """Compact component storage, independent of geometry and tile boundaries."""

    def __init__(self, count: int):
        import numpy as np

        self.parent = np.arange(count, dtype="int64")
        self.size = np.ones(count, dtype="int64")

    def find(self, a: int) -> int:
        while self.parent[a] != a:
            self.parent[a] = self.parent[self.parent[a]]
            a = int(self.parent[a])
        return a

    def union(self, a: int, b: int):
        a, b = self.find(a), self.find(b)
        if a == b:
            return
        if self.size[a] < self.size[b]:
            a, b = b, a
        self.parent[b] = a
        self.size[a] += self.size[b]
