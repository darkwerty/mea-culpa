"""Genera backend/meaculpa/geo/paises_sv.json: países con buena cobertura de Street View y su contorno simplificado.

Fuente: Natural Earth 1:50m, «Admin 0 Countries» (dominio público), en GeoJSON:
https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/ne_50m_admin_0_countries.geojson

Uso (desde backend/):
    python scripts/generar_paises_sv.py ruta/ne_50m_admin_0_countries.geojson

El fichero resultante lleva, por país: código ISO, nombre en castellano, continente, peso dentro de su continente y
los polígonos simplificados (Douglas-Peucker, 0,02 grados, coordenadas con 3 decimales). Solo se usan para elegir
puntos al azar en tierra firme (sin mares) y para comprobar en qué país ha caído la panorámica. No sale del servidor.
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "meaculpa" / "geo" / "paises_sv.json"
TOLERANCE = 0.02
MIN_RING_AREA = 0.02  # grados²: islotes que no aportan nada

# Reparto por continentes (suma 100) y peso de cada país dentro de su continente. Solo países con cobertura amplia
# de Street View oficial de Google; los pesos son a ojo: tamaño, variedad de paisaje y cuánto está cubierto.
CONTINENTS = {"Europa": 30, "Norteamérica": 18, "Sudamérica": 14, "Asia": 22, "África": 9, "Oceanía": 7}
# fmt: off
COUNTRIES: dict[str, tuple[str, float]] = {
    # Europa
    "ES": ("Europa", 3), "PT": ("Europa", 1.5), "FR": ("Europa", 3), "IT": ("Europa", 2.5), "DE": ("Europa", 2.5),
    "GB": ("Europa", 2.5), "IE": ("Europa", 1), "NL": ("Europa", 1), "BE": ("Europa", 0.8), "LU": ("Europa", 0.2),
    "CH": ("Europa", 1), "AT": ("Europa", 1), "PL": ("Europa", 1.5), "CZ": ("Europa", 1), "SK": ("Europa", 0.6),
    "HU": ("Europa", 0.8), "RO": ("Europa", 1.2), "BG": ("Europa", 0.8), "GR": ("Europa", 1), "HR": ("Europa", 0.8),
    "SI": ("Europa", 0.4), "RS": ("Europa", 0.6), "ME": ("Europa", 0.3), "AL": ("Europa", 0.4), "MK": ("Europa", 0.3),
    "DK": ("Europa", 0.8), "SE": ("Europa", 1.5), "NO": ("Europa", 1.5), "FI": ("Europa", 1.2), "EE": ("Europa", 0.4),
    "LV": ("Europa", 0.4), "LT": ("Europa", 0.4), "IS": ("Europa", 0.6), "TR": ("Europa", 1.5),
    # Norteamérica (con Centroamérica y el Caribe)
    "US": ("Norteamérica", 10), "CA": ("Norteamérica", 4), "MX": ("Norteamérica", 3.5), "GT": ("Norteamérica", 0.5),
    "CR": ("Norteamérica", 0.4), "PA": ("Norteamérica", 0.3), "DO": ("Norteamérica", 0.4), "PR": ("Norteamérica", 0.2),
    # Sudamérica
    "BR": ("Sudamérica", 5), "AR": ("Sudamérica", 3), "CL": ("Sudamérica", 2.5), "CO": ("Sudamérica", 1.5),
    "PE": ("Sudamérica", 1.2), "EC": ("Sudamérica", 0.8), "UY": ("Sudamérica", 0.6), "BO": ("Sudamérica", 0.8),
    # Asia
    "JP": ("Asia", 4), "TW": ("Asia", 1), "TH": ("Asia", 2.5), "MY": ("Asia", 1.5), "ID": ("Asia", 3),
    "PH": ("Asia", 2), "KH": ("Asia", 0.6), "LA": ("Asia", 0.4), "IN": ("Asia", 3), "LK": ("Asia", 0.6),
    "BD": ("Asia", 0.6), "MN": ("Asia", 0.6), "IL": ("Asia", 0.6), "JO": ("Asia", 0.4), "AE": ("Asia", 0.4),
    "KG": ("Asia", 0.5), "KZ": ("Asia", 1), "BT": ("Asia", 0.2), "RU": ("Asia", 2),
    # África
    "ZA": ("África", 3), "BW": ("África", 0.8), "LS": ("África", 0.4), "SZ": ("África", 0.3), "KE": ("África", 1),
    "UG": ("África", 0.8), "NG": ("África", 0.6), "GH": ("África", 0.8), "SN": ("África", 0.6), "TN": ("África", 0.5),
    "RW": ("África", 0.3), "MG": ("África", 0.3),
    # Oceanía
    "AU": ("Oceanía", 4.5), "NZ": ("Oceanía", 2.5),
}
# fmt: on


def _perp(p, a, b) -> float:
    (x, y), (x1, y1), (x2, y2) = p, a, b
    dx, dy = x2 - x1, y2 - y1
    if dx == dy == 0:
        return math.hypot(x - x1, y - y1)
    return abs(dy * x - dx * y + x2 * y1 - y2 * x1) / math.hypot(dx, dy)


def simplify(points: list[list[float]], tol: float) -> list[list[float]]:
    """Douglas-Peucker iterativo."""
    if len(points) < 4:
        return points
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        i, j = stack.pop()
        best, idx = 0.0, -1
        for k in range(i + 1, j):
            d = _perp(points[k], points[i], points[j])
            if d > best:
                best, idx = d, k
        if best > tol and idx > 0:
            keep[idx] = True
            stack += [(i, idx), (idx, j)]
    return [p for p, k in zip(points, keep, strict=True) if k]


def ring_area(ring: list[list[float]]) -> float:
    return abs(sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1], strict=True))) / 2


def main(src: str) -> None:
    data = json.loads(Path(src).read_text(encoding="utf-8"))
    out = []
    found = set()
    for feat in data["features"]:
        p = feat["properties"]
        code = p.get("ISO_A2_EH") or p.get("ISO_A2")
        if code not in COUNTRIES or code in found:
            continue
        geom = feat["geometry"]
        polys = geom["coordinates"] if geom["type"] == "MultiPolygon" else [geom["coordinates"]]
        clean = []
        for poly in polys:
            rings = []
            for i, ring in enumerate(poly):
                s = [[round(x, 3), round(y, 3)] for x, y in simplify(ring, TOLERANCE)]
                if len(s) >= 4 and (i > 0 or ring_area(s) >= MIN_RING_AREA):
                    rings.append(s)
                elif i == 0:
                    break
            if rings:
                clean.append(rings)
        continent, weight = COUNTRIES[code]
        found.add(code)
        out.append(
            {
                "codigo": code,
                "nombre": p.get("NAME_ES") or p.get("NAME"),
                "continente": continent,
                "peso": weight,
                "poligonos": clean,
            }
        )
    missing = sorted(set(COUNTRIES) - found)
    if missing:
        print("Sin contorno en Natural Earth:", ", ".join(missing))
    out.sort(key=lambda c: (c["continente"], c["codigo"]))
    payload = {"fuente": "Natural Earth 1:50m Admin 0 Countries (dominio público)", "continentes": CONTINENTS}
    payload["paises"] = out
    OUT.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"{len(out)} países, {OUT.stat().st_size // 1024} KB en {OUT}")


if __name__ == "__main__":
    main(sys.argv[1])
