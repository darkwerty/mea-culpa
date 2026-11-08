"""Almacén compartido (Blob en Azure, ficheros en local) con concurrencia optimista por ETag."""

from __future__ import annotations

import datetime as dt
import itertools

import pytest
from azure.core.exceptions import ResourceExistsError, ResourceModifiedError, ResourceNotFoundError

from meaculpa.storage import AzureBlobObjectStore, FileObjectStore


class FakeBlobContainer:
    """Lo mínimo de azure.storage.blob.ContainerClient, con la semántica de ETag del servicio."""

    def __init__(self) -> None:
        self.blobs: dict[str, tuple[bytes, str, dt.datetime]] = {}
        self._etags = itertools.count(1)

    def download_blob(self, name):
        if name not in self.blobs:
            raise ResourceNotFoundError("no existe")
        data, etag, _ = self.blobs[name]
        return type("Downloader", (), {"readall": lambda self: data, "properties": type("P", (), {"etag": etag})})()

    def get_blob_client(self, name):
        container = self

        class BlobClient:
            def upload_blob(self, data, overwrite=False, etag=None, match_condition=None):
                current = container.blobs.get(name)
                if current and not overwrite:
                    raise ResourceExistsError("ya existe")
                if etag is not None and (current is None or current[1] != etag):
                    raise ResourceModifiedError("etag distinto")
                new = f'"0x{next(container._etags)}"'
                container.blobs[name] = (data, new, dt.datetime.now(dt.UTC))
                return {"etag": new}

        return BlobClient()

    def list_blobs(self, name_starts_with=""):
        return [
            type("B", (), {"name": n, "last_modified": m})()
            for n, (_, _, m) in self.blobs.items()
            if n.startswith(name_starts_with)
        ]


@pytest.fixture(params=["ficheros", "blob"])
def store(request, tmp_path):
    if request.param == "ficheros":
        return FileObjectStore(tmp_path)
    return AzureBlobObjectStore("https://x.blob.core.windows.net", "meaculpa", container_client=FakeBlobContainer())


def test_create_only_once_then_update_with_etag(store):
    etag = store.write("games/abc/1.json", b"uno")
    assert etag
    assert store.write("games/abc/1.json", b"otra vez") is None  # ya existe
    data, current = store.read("games/abc/1.json")
    assert data == b"uno" and current == etag
    new = store.write("games/abc/1.json", b"dos", etag=etag)
    assert new and new != etag
    assert store.write("games/abc/1.json", b"tres", etag=etag) is None  # etag viejo: conflicto
    assert store.read("games/abc/1.json")[0] == b"dos"
    assert store.read("games/abc/2.json") is None


def test_list_by_prefix(store):
    store.write("games/aaa/1.json", b"1")
    store.write("games/aaa/2.json", b"2")
    store.write("games/bbb/3.json", b"3")
    assert sorted(k for k, _ in store.list("games/aaa")) == ["games/aaa/1.json", "games/aaa/2.json"]


@pytest.mark.parametrize("key", ["../fuera", "games/../x", "Games/A", "games/a b", "/abs"])
def test_keys_are_validated(store, key):
    with pytest.raises(ValueError):
        store.read(key)
