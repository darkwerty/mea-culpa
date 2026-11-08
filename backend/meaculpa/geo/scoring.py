"""Distancia en la esfera y puntuación de cada ronda."""

from __future__ import annotations

import math

EARTH_RADIUS_KM = 6371.0088  # radio medio (IUGG)
MAX_POINTS = 5000
SCALE_KM = 2000.0


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(a)))


def score(distance_km: float) -> int:
    """5000 en el sitio exacto; ~3033 a 1000 km, ~1839 a 2000 km, ~68 a 8600 km."""
    if distance_km < 0 or math.isnan(distance_km):
        raise ValueError("distancia no válida")
    return round(MAX_POINTS * math.exp(-distance_km / SCALE_KM))
