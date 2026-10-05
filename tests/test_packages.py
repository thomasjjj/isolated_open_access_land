import os

import pytest

from access_islands import packages
from access_islands.common import atomic_json, read_json


def completed(path, signature):
    path.mkdir(parents=True)
    atomic_json(path / "run_manifest.json", {"complete": True, "signature": signature})
    atomic_json(path / "validation.json", {"complete": True, "analysis_signature": signature})
    (path / "sites.csv").write_text(signature, encoding="utf-8")


def test_completed_run_publishes_after_a_transient_windows_sharing_lock(tmp_path, monkeypatch):
    working, output = tmp_path / "working", tmp_path / "county"
    completed(working, "new")
    output.mkdir()
    original = os.replace
    locked = False

    def replace(source, target):
        nonlocal locked
        if str(source).endswith(".partial") and not locked:
            locked = True
            error = PermissionError("Temporary sharing lock")
            error.winerror = 32
            raise error
        original(source, target)

    monkeypatch.setattr(packages.os, "replace", replace)
    target = packages.publish_completed(working, output)
    assert locked
    assert (target / "sites.csv").read_text(encoding="utf-8") == "new"
    assert read_json(output / "latest_completed.json")["signature"] == "new"
    assert not target.with_name("new.partial").exists()


def test_persistent_windows_lock_does_not_replace_previous_completed_run(tmp_path, monkeypatch):
    working, output = tmp_path / "working", tmp_path / "county"
    completed(output, "old")
    completed(working, "new")
    packages.preserve_completed(output)
    original = os.replace

    def replace(source, target):
        if str(source).endswith(".partial"):
            error = PermissionError("Persistent lock")
            error.winerror = 5
            raise error
        original(source, target)

    monkeypatch.setattr(packages, "DIRECTORY_REPLACE_TIMEOUT", 0)
    monkeypatch.setattr(packages.os, "replace", replace)
    with pytest.raises(PermissionError):
        packages.publish_completed(working, output)
    assert (output / "sites.csv").read_text(encoding="utf-8") == "old"
    assert not (output / "runs/new").exists()
