import zipfile

import pytest
import requests

from access_islands.sources import acquire_arcgis, download_file, extract_archive


class Response:
    def __init__(self, value):
        self.value = value

    def raise_for_status(self):
        pass

    def json(self):
        return self.value


class Service:
    def __init__(self, missing=False):
        self.missing = missing

    def get(self, url, params=None, **kwargs):
        if not url.endswith("query"):
            return Response({"objectIdField": "OBJECTID", "maxRecordCount": 1})
        if params.get("returnIdsOnly"):
            return Response({"objectIds": [1, 2]})
        ids = [int(x) for x in params["objectIds"].split(",")]
        if self.missing:
            ids = []
        return Response(
            {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "properties": {"OBJECTID": i},
                        "geometry": {
                            "type": "Polygon",
                            "coordinates": [[[i, 0], [i + 0.1, 0], [i + 0.1, 0.1], [i, 0.1], [i, 0]]],
                        },
                    }
                    for i in ids
                ],
            }
        )


def test_service_pages_verified(tmp_path):
    meta = acquire_arcgis(Service(), "https://example.test/0", tmp_path / "data.gpkg")
    assert meta["feature_count"] == 2


def test_missing_service_ids_fails_atomically(tmp_path):
    with pytest.raises(ValueError, match="Missing feature IDs"):
        acquire_arcgis(Service(missing=True), "https://example.test/0", tmp_path / "data.gpkg")
    assert not (tmp_path / "data.gpkg").exists()


def test_archive_traversal_rejected(tmp_path):
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("../escape.txt", "bad")
    with pytest.raises(ValueError, match="Unsafe archive"):
        extract_archive(archive, tmp_path / "extract")


def test_fid_field_survives_multiple_batches(tmp_path):
    import geopandas as gpd

    class FidService(Service):
        def get(self, url, params=None, **kwargs):
            result = super().get(url, params=params, **kwargs).value
            if "objectIdField" in result:
                result["objectIdField"] = "FID"
            for feature in result.get("features", []):
                feature["properties"]["FID"] = feature["properties"].pop("OBJECTID")
            return Response(result)

    target = tmp_path / "data.gpkg"
    acquire_arcgis(FidService(), "https://example.test/0", target)
    assert list(gpd.read_file(target).source_fid) == [1, 2]


def test_oversized_service_batch_is_split(tmp_path):
    class LargeService(Service):
        def get(self, url, params=None, **kwargs):
            if params and len(params.get("objectIds", "").split(",")) > 1:
                raise requests.RequestException("Oversized response")
            result = super().get(url, params=params, **kwargs).value
            if "maxRecordCount" in result:
                result["maxRecordCount"] = 10
            return Response(result)

    assert (
        acquire_arcgis(LargeService(), "https://example.test/0", tmp_path / "data.gpkg")["feature_count"] == 2
    )


def test_interrupted_download_resumes_verified_range(tmp_path, monkeypatch):
    monkeypatch.setattr("access_islands.sources.time.sleep", lambda seconds: None)

    class Stream:
        def __init__(self, first):
            self.first = first
            self.status_code = 200 if first else 206
            self.headers = {"Content-Length": "6" if first else "3", "ETag": "stable"}
            if not first:
                self.headers["Content-Range"] = "bytes 3-5/6"

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def raise_for_status(self):
            pass

        def iter_content(self, size):
            yield b"abc" if self.first else b"def"
            if self.first:
                raise requests.exceptions.ChunkedEncodingError("interrupted")

    class Download:
        def __init__(self):
            self.headers = []

        def get(self, url, headers=None, **kwargs):
            self.headers.append(headers)
            return Stream(len(self.headers) == 1)

    s = Download()
    target = tmp_path / "blob.bin"
    download_file(s, "https://example.test/blob", target)
    assert target.read_bytes() == b"abcdef"
    assert s.headers[1]["Range"] == "bytes=3-"
    assert s.headers[1]["If-Range"] == "stable"


def test_folder_input_changes_are_fingerprinted(tmp_path):
    from access_islands.common import source_digest

    folder = tmp_path / "routes.gdb"
    folder.mkdir()
    table = folder / "table.bin"
    table.write_bytes(b"before")
    first = source_digest(folder)
    table.write_bytes(b"after")
    assert source_digest(folder) != first
