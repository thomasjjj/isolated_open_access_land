"""Completed packages are immutable; the county root is a convenience copy."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from .common import atomic_json, read_json


def preserve_completed(output):
    manifest = read_json(output / "run_manifest.json", {})
    validation = read_json(output / "validation.json", {})
    if not manifest.get("complete") or not validation.get("complete"):
        return None
    target = output / "runs" / manifest["signature"]
    if not target.exists():
        target.parent.mkdir(exist_ok=True)
        scratch = target.with_name(target.name + ".partial")
        scratch.mkdir(exist_ok=True)
        for item in output.iterdir():
            if item.name in {"runs", "_working", "research.lock", "latest_completed.json"}:
                continue
            if item.is_file():
                shutil.copy2(item, scratch / item.name)
            elif item.name == "map":
                shutil.copytree(item, scratch / item.name, dirs_exist_ok=True)
        os.replace(scratch, target)
    return target


def publish_completed(working, output):
    manifest = read_json(working / "run_manifest.json")
    validation = read_json(working / "validation.json")
    if (
        not manifest.get("complete")
        or not validation.get("complete")
        or validation.get("analysis_signature") != manifest["signature"]
    ):
        raise ValueError("Cannot publish an incomplete or unvalidated run")
    target = output / "runs" / manifest["signature"]
    target.parent.mkdir(exist_ok=True)
    if not target.exists():
        scratch = target.with_name(target.name + ".partial")
        shutil.copytree(working, scratch, dirs_exist_ok=True)
        os.replace(scratch, target)
    for item in target.rglob("*"):
        if not item.is_file():
            continue
        destination = output / item.relative_to(target)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(destination.name + ".publish.tmp")
        shutil.copy2(item, temporary)
        os.replace(temporary, destination)
    atomic_json(
        output / "latest_completed.json",
        {"signature": manifest["signature"], "package": str(target.resolve())},
    )
    return target


def report_package(output):
    latest = read_json(output / "latest_completed.json", {})
    return Path(latest["package"]) if latest else output
