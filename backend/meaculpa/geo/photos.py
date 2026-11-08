"""Conjunto de fotos curadas (data/fotos/manifest.json).

El manifest lleva la solución y la atribución; nunca se manda al navegador entero. Al cliente solo le
llega la imagen de la ronda en curso (por número de ronda, sin id) y, tras responder, la solución y la
atribución de esa foto.
"""

from __future__ import annotations

import json
import random
import re
from dataclasses import dataclass
from pathlib import Path

_ID = re.compile(r"^[0-9a-f]{12}$")


@dataclass(frozen=True)
class Photo:
    id: str
    fichero: str
    pais: str
    region: str
    lat: float
    lon: float
    autor: str
    licencia: str
    licencia_url: str | None
    origen: str
    titulo: str

    def solution(self) -> dict:
        return {"pais": self.pais, "region": self.region, "lat": self.lat, "lon": self.lon}

    def attribution(self) -> dict:
        return {
            "autor": self.autor,
            "licencia": self.licencia,
            "licencia_url": self.licencia_url,
            "origen": self.origen,
            "titulo": self.titulo,
        }


class PhotoSet:
    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)
        manifest = self.directory / "manifest.json"
        rows = json.loads(manifest.read_text(encoding="utf-8")) if manifest.exists() else []
        self.photos: dict[str, Photo] = {}
        for row in rows:
            p = Photo(**{k: row.get(k) for k in Photo.__dataclass_fields__})
            if not _ID.match(p.id) or p.fichero != f"{p.id}.jpg":
                raise ValueError(f"Entrada del manifest no válida: {p.id!r}")
            self.photos[p.id] = p

    def __len__(self) -> int:
        return len(self.photos)

    def get(self, photo_id: str) -> Photo:
        return self.photos[photo_id]

    def image_bytes(self, photo_id: str) -> bytes:
        p = self.photos[photo_id]  # solo ids del manifest: nunca una ruta que venga del cliente
        return (self.directory / p.fichero).read_bytes()

    def pick(self, n: int, avoid: set[str], rng: random.Random | None = None) -> list[str]:
        """`n` fotos al azar, primero entre las que el usuario no ha visto."""
        rng = rng or random.SystemRandom()
        fresh = [i for i in self.photos if i not in avoid]
        seen = [i for i in self.photos if i in avoid]
        rng.shuffle(fresh)
        rng.shuffle(seen)
        chosen = (fresh + seen)[:n]
        if len(chosen) < n:
            raise ValueError("No hay fotos suficientes")
        return chosen
