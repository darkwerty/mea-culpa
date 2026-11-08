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


# ------------------------------------------------------------------ Street View falso (sin red)
SV_KEY = "clave-servidor-de-prueba-XYZ"


class FakeGoogle:
    """Sustituye a las peticiones HTTP a Google: metadatos (por ubicación o por pano) y capturas de Street View Static.

    Cada búsqueda por ubicación devuelve una panorámica en ese mismo punto. `user_every`: una de cada tantas es de un
    usuario (copyright ajeno, pano largo) y se debe descartar. `fail`: Google no responde.
    """

    def __init__(self, user_every: int = 0, fail: bool = False) -> None:
        self.user_every = user_every
        self.fail = fail
        self.panos: dict[str, tuple[float, float]] = {}
        self.calls: list[tuple[str, dict]] = []
        self.static = 0

    def __call__(self, url: str, params: dict) -> tuple[int, bytes, str]:
        assert params.get("key") == SV_KEY
        self.calls.append((url, dict(params)))
        if self.fail:
            raise ConnectionError(f"sin red al pedir {url}?key={params['key']}")
        if url.endswith("/metadata"):
            if "pano" in params:
                loc = self.panos.get(params["pano"])
                if loc is None:
                    return 200, json.dumps({"status": "ZERO_RESULTS"}).encode(), "application/json"
                return 200, self._meta(params["pano"], *loc), "application/json"
            assert params["source"] == "google" and int(params["radius"]) > 0
            lat, lon = (float(x) for x in params["location"].split(","))
            n = len(self.panos) + 1
            if self.user_every and n % self.user_every == 0:
                body = {"status": "OK", "copyright": "© Persona", "pano_id": "CAoSL" + "x" * 30, "location": {}}
                body["location"] = {"lat": lat, "lng": lon}
                self.panos[f"user{n}"] = (lat, lon)
                return 200, json.dumps(body).encode(), "application/json"
            pano = f"P{n:021d}"
            self.panos[pano] = (round(lat, 6), round(lon, 6))
            return 200, self._meta(pano, lat, lon), "application/json"
        self.static += 1
        assert params["pano"] in self.panos and params["return_error_code"] == "true"
        return 200, b"\xff\xd8\xff\xe0captura-" + f"{params['pano']}-{params['heading']}".encode(), "image/jpeg"

    @staticmethod
    def _meta(pano: str, lat: float, lon: float) -> bytes:
        return json.dumps(
            {
                "status": "OK",
                "copyright": "© 2026 Google",
                "date": "2024-05",
                "pano_id": pano,
                "location": {"lat": lat, "lng": lon},
            }
        ).encode()
