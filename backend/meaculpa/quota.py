"""Cuotas diarias por usuario y fusible global de llamadas al LLM.

Los contadores viven en el mismo almacén que las partidas (Blob en Azure), así que valen para todas las
réplicas y no se reinician al escalar a cero. Se actualizan con concurrencia optimista (ETag).
"""

from __future__ import annotations

import json
import time

from fastapi import HTTPException

from .config import Settings
from .storage import ObjectStore

KIND_LABELS = {
    "partidas": "partidas nuevas",
    "jugadas": "jugadas",
    "jugadas_llm": "jugadas contra la IA",
    "analisis": "análisis",
}


class UsageLimiter:
    MAX_RETRIES = 8

    def __init__(self, objects: ObjectStore, settings: Settings, clock=time.time) -> None:
        self.objects = objects
        self.clock = clock
        self.limits = {
            "partidas": settings.quota_games_per_day,
            "jugadas": settings.quota_moves_per_day,
            "jugadas_llm": settings.quota_llm_moves_per_day,
            "analisis": settings.quota_analyses_per_day,
        }
        self.global_llm_limit = settings.global_llm_calls_per_day

    def _day(self) -> str:
        return time.strftime("%Y-%m-%d", time.gmtime(self.clock()))

    def _increment(self, key: str, field: str, amount: int, limit: int) -> bool:
        """Suma `amount` si no se pasa de `limit`. False si se pasaría."""
        for _ in range(self.MAX_RETRIES):
            found = self.objects.read(key)
            counts, etag = (json.loads(found[0]), found[1]) if found else ({}, None)
            current = int(counts.get(field, 0))
            if current + amount > limit:
                return False
            counts[field] = current + amount
            if self.objects.write(key, json.dumps(counts).encode(), etag=etag) is not None:
                return True
        raise HTTPException(503, "Demasiadas peticiones a la vez; vuelve a intentarlo")

    def consume(self, owner: str, kind: str, amount: int = 1) -> None:
        limit = self.limits[kind]
        if not self._increment(f"usage/{self._day()}/{owner}", kind, amount, limit):
            raise HTTPException(429, f"Has llegado al límite diario de {KIND_LABELS[kind]} ({limit}). Vuelve mañana.")

    def consume_llm_calls(self, calls: int) -> None:
        """Fusible de coste: llamadas al LLM de todos los usuarios en el día (UTC)."""
        if not self._increment(f"usage/{self._day()}/global", "llm_calls", calls, self.global_llm_limit):
            raise HTTPException(503, "La demo ha agotado su presupuesto diario de IA. Vuelve mañana.")
