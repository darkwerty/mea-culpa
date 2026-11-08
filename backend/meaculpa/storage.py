"""Almacén de objetos con control de concurrencia optimista (ETag).

- En Azure: Blob Storage con la identidad administrada (sin claves). Todas las réplicas ven lo mismo y
  sobrevive al escalado a cero.
- En local y en los tests: ficheros bajo `data_dir`.

Las claves las construye siempre el servidor (hash del usuario + id hexadecimal), nunca el cliente.
"""

from __future__ import annotations

import contextlib
import hashlib
import re
import threading
from pathlib import Path
from typing import Protocol

from .config import Settings

_KEY = re.compile(r"^[a-z0-9_\-]+(/[a-z0-9_\-.]+)*$")


def _check_key(key: str) -> str:
    if not _KEY.match(key) or ".." in key:
        raise ValueError(f"Clave no válida: {key!r}")
    return key


class ObjectStore(Protocol):
    def read(self, key: str) -> tuple[bytes, str] | None:
        """(contenido, etag) o None si no existe."""

    def write(self, key: str, data: bytes, etag: str | None = None) -> str | None:
        """Escribe y devuelve el nuevo etag. Con `etag`, solo si no ha cambiado; con etag=None, solo si no
        existe. None si hay conflicto."""

    def list(self, prefix: str) -> list[tuple[str, float]]:
        """(clave, última modificación en epoch) de las claves que empiezan por `prefix`."""

    def delete(self, key: str) -> None:
        """Borra la clave (sin error si no existe)."""


class FileObjectStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def _path(self, key: str) -> Path:
        return self.root.joinpath(*_check_key(key).split("/"))

    @staticmethod
    def _etag(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    def read(self, key: str) -> tuple[bytes, str] | None:
        path = self._path(key)
        with self._lock:
            if not path.exists():
                return None
            data = path.read_bytes()
        return data, self._etag(data)

    def write(self, key: str, data: bytes, etag: str | None = None) -> str | None:
        path = self._path(key)
        with self._lock:
            if path.exists():
                if etag is None or self._etag(path.read_bytes()) != etag:
                    return None
            elif etag is not None:
                return None
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_bytes(data)
            tmp.replace(path)
            return self._etag(data)

    def list(self, prefix: str) -> list[tuple[str, float]]:
        base = self.root.joinpath(*_check_key(prefix.rstrip("/")).split("/"))
        if not base.exists():
            return []
        out = []
        for path in base.rglob("*"):
            if path.is_file() and not path.name.endswith(".tmp"):
                out.append((path.relative_to(self.root).as_posix(), path.stat().st_mtime))
        return out

    def delete(self, key: str) -> None:
        with self._lock:
            self._path(key).unlink(missing_ok=True)


class AzureBlobObjectStore:
    def __init__(self, account_url: str, container: str, container_client=None) -> None:
        if container_client is None:
            from azure.identity import DefaultAzureCredential
            from azure.storage.blob import ContainerClient

            container_client = ContainerClient(account_url, container, credential=DefaultAzureCredential())
        self._container = container_client

    def read(self, key: str) -> tuple[bytes, str] | None:
        from azure.core.exceptions import ResourceNotFoundError

        try:
            downloader = self._container.download_blob(_check_key(key))
        except ResourceNotFoundError:
            return None
        return downloader.readall(), downloader.properties.etag

    def write(self, key: str, data: bytes, etag: str | None = None) -> str | None:
        from azure.core import MatchConditions
        from azure.core.exceptions import ResourceExistsError, ResourceModifiedError

        kwargs = (
            {"overwrite": True, "etag": etag, "match_condition": MatchConditions.IfNotModified}
            if etag is not None
            else {"overwrite": False}
        )
        try:
            result = self._container.get_blob_client(_check_key(key)).upload_blob(data, **kwargs)
        except (ResourceExistsError, ResourceModifiedError):
            return None
        return result["etag"]

    def list(self, prefix: str) -> list[tuple[str, float]]:
        return [
            (b.name, b.last_modified.timestamp())
            for b in self._container.list_blobs(name_starts_with=_check_key(prefix.rstrip("/")) + "/")
        ]

    def delete(self, key: str) -> None:
        from azure.core.exceptions import ResourceNotFoundError

        with contextlib.suppress(ResourceNotFoundError):
            self._container.delete_blob(_check_key(key))


def make_object_store(settings: Settings) -> ObjectStore:
    if settings.storage_account_url:
        return AzureBlobObjectStore(settings.storage_account_url, settings.storage_container)
    return FileObjectStore(settings.data_dir / "store")
