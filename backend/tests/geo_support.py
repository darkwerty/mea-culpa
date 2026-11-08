"""Apoyo de los tests de ¿Dónde está esta foto? (router /api/geo): fotos falsas y tokens de prueba."""

from __future__ import annotations

import json
import time
from pathlib import Path

from meaculpa.geo.photos import PhotoSet

GEO_CLIENT_ID = "test-client"

# Soluciones de las fotos de prueba: valores únicos para poder buscarlos en las respuestas.
FAKE_PHOTOS = [
    ("a1b2c3d4e5f6", "Paisdeprueba Uno", "Regionsecreta Uno", 12.34567, 45.67891),
    ("b1b2c3d4e5f6", "Paisdeprueba Dos", "Regionsecreta Dos", -33.21987, 151.12345),
    ("c1b2c3d4e5f6", "Paisdeprueba Tres", "Regionsecreta Tres", 64.13579, -21.97531),
    ("d1b2c3d4e5f6", "Paisdeprueba Cuatro", "Regionsecreta Cuatro", 35.02468, 135.86421),
    ("e1b2c3d4e5f6", "Paisdeprueba Cinco", "Regionsecreta Cinco", -13.16302, -72.54497),
    ("f1b2c3d4e5f6", "Paisdeprueba Seis", "Regionsecreta Seis", 40.41678, -3.70379),
]


def make_photos(directory: Path) -> PhotoSet:
    directory.mkdir(parents=True, exist_ok=True)
    rows = []
    for i, (pid, pais, region, lat, lon) in enumerate(FAKE_PHOTOS):
        # Bytes distintos con cabecera JPEG: el juego no decodifica la imagen, solo la sirve y la manda al modelo.
        (directory / f"{pid}.jpg").write_bytes(b"\xff\xd8\xff\xe0" + f"foto-{i}-{pid}".encode() + b"\xff\xd9")
        rows.append(
            {
                "id": pid,
                "fichero": f"{pid}.jpg",
                "pais": pais,
                "region": region,
                "lat": lat,
                "lon": lon,
                "autor": f"Autorsecreto {i}",
                "licencia": "CC BY-SA 4.0",
                "licencia_url": "https://creativecommons.org/licenses/by-sa/4.0",
                "origen": f"https://commons.wikimedia.org/wiki/File:Lugarsecreto_{i}.jpg",
                "titulo": f"Lugarsecreto_{i}.jpg",
            }
        )
    (directory / "manifest.json").write_text(json.dumps(rows), encoding="utf-8")
    return PhotoSet(directory)


def geo_verifier(token: str) -> dict:
    """Token de prueba = sub del usuario; 'malo' simula una firma no válida."""
    if token == "malo":
        raise ValueError("firma no válida")
    return {
        "aud": GEO_CLIENT_ID,
        "iss": "https://accounts.google.com",
        "exp": time.time() + 3600,
        "sub": token,
        "email_verified": True,
        "given_name": token,
    }


def geo_auth(user: str = "ana") -> dict:
    return {"Authorization": f"Bearer {user}"}
