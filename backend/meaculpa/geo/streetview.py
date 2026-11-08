"""Street View para «¿Dónde está?»: puntos al azar, metadatos, reserva de pano_id y capturas para la IA.

Condiciones de Google Maps Platform que se cumplen aquí (EEA Service Specific Terms y Terms of Service):
- Solo se guarda el pano_id (sección «Google ID Caching»: se puede guardar sin plazo). Las coordenadas que devuelven
  los metadatos no se guardan nunca: se piden otra vez (los metadatos son gratis) cada vez que hacen falta para
  puntuar o enseñar la solución, y se descartan al terminar la petición.
- Las capturas de Street View Static no se guardan ni se enseñan: viajan en memoria al modelo de IA (solo
  inferencia, nada de entrenamiento) y se sueltan al acabar la ronda. Nunca se pintan junto a un mapa.
- Solo panorámicas oficiales de Google (`source=google` y copyright de Google): nada de fotos subidas por usuarios.
- La clave del servidor no sale de este módulo: no va en respuestas, ni en registros, ni en mensajes de error.

El país de cada panorámica sale de datos propios (contornos de Natural Earth, dominio público, en paises_sv.json):
el punto al azar se elige dentro del país y la panorámica solo se acepta si cae dentro de ese mismo país.
"""

from __future__ import annotations

import calendar
import json
import logging
import math
import random
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..storage import ObjectStore
from .config import GeoSettings

log = logging.getLogger("meaculpa.geo.streetview")

METADATA_URL = "https://maps.googleapis.com/maps/api/streetview/metadata"
STATIC_URL = "https://maps.googleapis.com/maps/api/streetview"
COUNTRIES_FILE = Path(__file__).with_name("paises_sv.json")
POOL_KEY = "geo/sv/pool"
MAX_POOL = 400
OFFICIAL_PANO_LEN = 22  # los pano_id de Google tienen 22 caracteres; los de usuarios son más largos (CAoS..., AF1Q...)

HttpGet = Callable[[str, dict], tuple[int, bytes, str]]


class StreetViewError(RuntimeError):
    """Google no ha respondido bien (sin la clave en el mensaje)."""


# ---------------------------------------------------------------------------------------------- día de Google
def _nth_sunday(year: int, month: int, n: int) -> int:
    first = calendar.weekday(year, month, 1)  # 0 = lunes
    first_sunday = 1 + (6 - first) % 7
    return first_sunday + 7 * (n - 1)


def pacific_day(ts: float) -> str:
    """Día en la hora de California (las cuotas diarias de Google se reinician a medianoche de allí).

    Horario de verano de EE. UU.: del segundo domingo de marzo al primer domingo de noviembre, a las 2:00 locales.
    """
    year = time.gmtime(ts).tm_year
    start = _utc(year, 3, _nth_sunday(year, 3, 2), 10)  # 2:00 PST = 10:00 UTC
    end = _utc(year, 11, _nth_sunday(year, 11, 1), 9)  # 2:00 PDT = 9:00 UTC
    offset = 7 if start <= ts < end else 8
    return time.strftime("%Y-%m-%d", time.gmtime(ts - offset * 3600))


def _utc(year: int, month: int, day: int, hour: int) -> float:
    return float(calendar.timegm((year, month, day, hour, 0, 0, 0, 0, 0)))


# ---------------------------------------------------------------------------------------------- países
def _inside(lon: float, lat: float, ring: list[list[float]]) -> bool:
    inside = False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i]
        xj, yj = ring[j]
        if (yi > lat) != (yj > lat) and lon < (xj - xi) * (lat - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def _in_polygon(lon: float, lat: float, poly: list[list[list[float]]]) -> bool:
    return _inside(lon, lat, poly[0]) and not any(_inside(lon, lat, hole) for hole in poly[1:])


def _area(ring: list[list[float]], lat_ref: float) -> float:
    a = abs(sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1], strict=True))) / 2
    return a * math.cos(math.radians(lat_ref))


@dataclass
class Country:
    code: str
    name: str
    continent: str
    weight: float
    polygons: list[list[list[list[float]]]]
    parts: list[tuple[float, tuple[float, float, float, float]]]  # (área, (minlon, minlat, maxlon, maxlat))

    def contains(self, lat: float, lon: float) -> bool:
        for poly, (_, (x0, y0, x1, y1)) in zip(self.polygons, self.parts, strict=True):
            if x0 <= lon <= x1 and y0 <= lat <= y1 and _in_polygon(lon, lat, poly):
                return True
        return False

    def random_point(self, rng: random.Random) -> tuple[float, float]:
        """Punto uniforme sobre la superficie (por área de cada trozo), siempre en tierra según el contorno."""
        weights = [a for a, _ in self.parts]
        for _ in range(200):
            idx = rng.choices(range(len(self.parts)), weights=weights)[0]
            poly, (x0, y0, x1, y1) = self.polygons[idx], self.parts[idx][1]
            for _ in range(60):
                lon, lat = rng.uniform(x0, x1), rng.uniform(y0, y1)
                if _in_polygon(lon, lat, poly):
                    return lat, lon
        raise RuntimeError(f"No se encuentra un punto en tierra en {self.code}")


class Countries:
    def __init__(self, path: Path = COUNTRIES_FILE) -> None:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        self.continents: dict[str, float] = data["continentes"]
        self.countries: dict[str, Country] = {}
        for row in data["paises"]:
            parts = []
            for poly in row["poligonos"]:
                xs = [p[0] for p in poly[0]]
                ys = [p[1] for p in poly[0]]
                parts.append((_area(poly[0], (min(ys) + max(ys)) / 2), (min(xs), min(ys), max(xs), max(ys))))
            self.countries[row["codigo"]] = Country(
                row["codigo"], row["nombre"], row["continente"], float(row["peso"]), row["poligonos"], parts
            )

    def name(self, code: str) -> str:
        c = self.countries.get(code)
        return c.name if c else code

    def pick(self, rng: random.Random) -> Country:
        """Continente según su reparto y, dentro, país según su peso."""
        conts = [c for c in self.continents if any(x.continent == c for x in self.countries.values())]
        cont = rng.choices(conts, weights=[self.continents[c] for c in conts])[0]
        options = [c for c in self.countries.values() if c.continent == cont]
        return rng.choices(options, weights=[c.weight for c in options])[0]

    def belongs(self, code: str, lat: float, lon: float) -> bool:
        """La panorámica cuenta como de `code` si cae dentro de su contorno, o fuera de todos (costa, islotes) y no
        dentro de otro país de la lista."""
        if self.countries[code].contains(lat, lon):
            return True
        return not any(c.contains(lat, lon) for k, c in self.countries.items() if k != code)


# ---------------------------------------------------------------------------------------------- cliente HTTP
def _requests_get(url: str, params: dict) -> tuple[int, bytes, str]:
    import requests

    r = requests.get(url, params=params, timeout=15)
    return r.status_code, r.content, r.headers.get("content-type", "")


class StreetViewClient:
    def __init__(self, key: str, http_get: HttpGet | None = None) -> None:
        self._key = key
        self._get = http_get or _requests_get

    def _call(self, url: str, params: dict) -> tuple[int, bytes, str]:
        try:
            return self._get(url, {**params, "key": self._key})
        except Exception as exc:  # noqa: BLE001 - el mensaje de requests puede llevar la URL con la clave
            raise StreetViewError(f"sin respuesta de Google ({type(exc).__name__})") from None

    def metadata(self, **params: Any) -> dict[str, Any]:
        """Metadatos de Street View (gratis, sin cuota): por pano o por ubicación y radio."""
        status, body, _ = self._call(METADATA_URL, params)
        if status != 200:
            raise StreetViewError(f"metadatos: HTTP {status}")
        try:
            data = json.loads(body)
        except ValueError:
            raise StreetViewError("metadatos: respuesta no válida") from None
        data.pop("error_message", None)  # puede repetir parámetros de la petición
        return data

    def search(self, lat: float, lon: float, radius_m: int) -> dict[str, Any]:
        return self.metadata(location=f"{lat:.6f},{lon:.6f}", radius=radius_m, source="google")

    def location(self, pano: str) -> tuple[float, float]:
        """Coordenadas de una panorámica, pedidas en el momento: no se guardan."""
        data = self.metadata(pano=pano)
        if data.get("status") != "OK" or "location" not in data:
            raise StreetViewError(f"metadatos: {data.get('status', 'sin estado')}")
        return float(data["location"]["lat"]), float(data["location"]["lng"])

    def image(self, pano: str, heading: float, fov: int, size: str) -> bytes:
        """Una captura de Street View Static (de pago): solo en memoria, para la IA."""
        status, body, ctype = self._call(
            STATIC_URL,
            {
                "pano": pano,
                "size": size,
                "heading": round(heading % 360, 1),
                "fov": fov,
                "pitch": 0,
                "return_error_code": "true",
            },
        )
        if status != 200 or not ctype.startswith("image/"):
            raise StreetViewError(f"captura: HTTP {status}")
        return body


def is_official(meta: dict[str, Any]) -> bool:
    """Panorámica oficial de Google al aire libre (nada de fotos de usuarios)."""
    pano = str(meta.get("pano_id") or "")
    return (
        meta.get("status") == "OK"
        and "Google" in str(meta.get("copyright") or "")
        and len(pano) == OFFICIAL_PANO_LEN
        and "location" in meta
    )


# ---------------------------------------------------------------------------------------------- reserva
class PanoPool:
    """Reserva de pano_id oficiales listos para jugar, en el almacén compartido (vale para todas las réplicas).

    Cada entrada: {"pano": pano_id, "pais": código ISO propio, "ts": cuándo se añadió}. Nada más: ni coordenadas
    ni imágenes. Cada pano_id se reparte una sola vez.
    """

    def __init__(
        self,
        objects: ObjectStore,
        client: StreetViewClient,
        geo: GeoSettings,
        countries: Countries | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self.objects = objects
        self.client = client
        self.geo = geo
        self.countries = countries or Countries()
        self.rng = rng or random.SystemRandom()
        self._lock = threading.Lock()
        self._running = False
        self.last_error: str | None = None

    def _read(self) -> tuple[list[dict[str, Any]], str | None]:
        found = self.objects.read(POOL_KEY)
        return (json.loads(found[0]).get("panos", []), found[1]) if found else ([], None)

    def size(self) -> int:
        return len(self._read()[0])

    def take(self, n: int) -> list[dict[str, Any]]:
        """Saca `n` entradas al azar (escritura condicionada: dos partidas a la vez no se llevan la misma)."""
        for _ in range(8):
            panos, etag = self._read()
            if len(panos) < n:
                return []
            idx = sorted(self.rng.sample(range(len(panos)), n))
            chosen = [panos[i] for i in idx]
            rest = [p for i, p in enumerate(panos) if i not in set(idx)]
            if self.objects.write(POOL_KEY, json.dumps({"panos": rest}).encode(), etag=etag) is not None:
                return chosen
        return []

    def _add(self, entries: list[dict[str, Any]]) -> None:
        for _ in range(8):
            panos, etag = self._read()
            known = {p["pano"] for p in panos}
            merged = (panos + [e for e in entries if e["pano"] not in known])[-MAX_POOL:]
            if self.objects.write(POOL_KEY, json.dumps({"panos": merged}).encode(), etag=etag) is not None:
                return

    def find_one(self) -> dict[str, Any] | None:
        """Un intento: país al azar, punto en tierra, panorámica oficial más cercana dentro del radio."""
        country = self.countries.pick(self.rng)
        lat, lon = country.random_point(self.rng)
        meta = self.client.search(lat, lon, self.geo.sv_radius_m)
        if not is_official(meta):
            return None
        loc = meta["location"]
        if not self.countries.belongs(country.code, float(loc["lat"]), float(loc["lng"])):
            return None  # ha caído al otro lado de una frontera: el país de la solución sería otro
        return {"pano": meta["pano_id"], "pais": country.code, "ts": round(time.time())}

    def refill(self, max_attempts: int = 400) -> int:
        """Rellena hasta `sv_pool_target`. Devuelve cuántas ha añadido. Se corta si Google falla varias veces."""
        target = self.geo.sv_pool_target
        have = self.size()
        added: list[dict[str, Any]] = []
        failures = 0
        for _ in range(max_attempts):
            if have + len(added) >= target:
                break
            try:
                entry = self.find_one()
                failures = 0
            except StreetViewError as exc:
                failures += 1
                self.last_error = str(exc)
                if failures >= 5:
                    log.warning("Street View no responde al rellenar la reserva: %s", exc)
                    break
                continue
            if entry and entry["pano"] not in {e["pano"] for e in added}:
                added.append(entry)
                if len(added) % 5 == 0:
                    self._add(added[-5:])
        rest = len(added) % 5
        if rest:
            self._add(added[-rest:])
        if added:
            log.info("Reserva de Street View: %d panorámicas nuevas", len(added))
        return len(added)

    def ensure(self) -> None:
        """Si queda poca reserva, la rellena (en un hilo aparte salvo en tests). Nunca dos rellenos a la vez."""
        try:
            if self.size() >= self.geo.sv_pool_low:
                return
        except Exception:  # noqa: BLE001 - el almacén no responde: ya se intentará en la siguiente partida
            log.exception("No se pudo leer la reserva de Street View")
            return
        with self._lock:
            if self._running:
                return
            self._running = True

        def run() -> None:
            try:
                self.refill()
            except Exception:  # noqa: BLE001 - el relleno nunca tumba la API
                log.exception("Fallo al rellenar la reserva de Street View")
            finally:
                with self._lock:
                    self._running = False

        if self.geo.sv_pool_background:
            threading.Thread(target=run, name="geo-sv-pool", daemon=True).start()
        else:
            run()

    def still_valid(self, entries: list[dict[str, Any]], max_age_s: float = 86_400) -> bool:
        """Las entradas antiguas se comprueban con los metadatos (gratis) antes de repartirlas."""
        old = [e for e in entries if time.time() - float(e.get("ts", 0)) > max_age_s]
        if not old:
            return True
        with ThreadPoolExecutor(max_workers=min(5, len(old))) as ex:
            metas = list(ex.map(lambda e: self.client.metadata(pano=e["pano"]), old))
        return all(m.get("status") == "OK" for m in metas)


def fetch_images(client: StreetViewClient, pano: str, base_heading: float, geo: GeoSettings) -> list[bytes]:
    """Las `sv_images` capturas de una ronda, repartidas por la vuelta entera, en paralelo y solo en memoria."""
    step = 360.0 / geo.sv_images
    headings = [base_heading + i * step for i in range(geo.sv_images)]
    with ThreadPoolExecutor(max_workers=geo.sv_images) as ex:
        return list(ex.map(lambda h: client.image(pano, h, geo.sv_fov, geo.sv_size), headings))
