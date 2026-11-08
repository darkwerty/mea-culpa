"""Cuotas diarias por usuario y fusible global de llamadas al LLM.

Los contadores viven en el mismo almacén que las partidas (Blob en Azure), así que valen para todas las
réplicas y no se reinician al escalar a cero. Se actualizan con concurrencia optimista (ETag).

Los límites salen de la configuración (variables de entorno) salvo que el panel /admin los cambie: `overrides`
recibe (tipo, valor por defecto) y devuelve el límite vigente.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable

from fastapi import HTTPException

from .config import Settings
from .storage import ObjectStore

KIND_LABELS = {
    "partidas": "partidas nuevas",
    "jugadas": "jugadas",
    "jugadas_llm": "jugadas contra la IA",
    "analisis": "análisis",
    "pistas": "pistas",
}


class UsageLimiter:
    MAX_RETRIES = 8

    def __init__(
        self,
        objects: ObjectStore,
        settings: Settings,
        clock=time.time,
        overrides: Callable[[str, int], int] | None = None,
    ) -> None:
        self.objects = objects
        self.clock = clock
        self.overrides = overrides
        self.limits = {
            "partidas": settings.quota_games_per_day,
            "jugadas": settings.quota_moves_per_day,
            "jugadas_llm": settings.quota_llm_moves_per_day,
            "analisis": settings.quota_analyses_per_day,
            "pistas": settings.quota_hints_per_day,
        }
        self.global_llm_limit = settings.global_llm_calls_per_day
        # Fusible de gasto: céntimos de dólar al día entre las dos demos (ajedrez y fotos), con lo que cuesta de
        # verdad cada llamada según los tokens. Se puede cambiar desde /admin (cuota «ia_centimos_dia»).
        self.global_cents_limit = settings.global_llm_cents_per_day
        # Y por persona (cuota «ia_centimos_usuario»), para que una sola no agote el del día con un modelo caro.
        self.user_cents_limit = settings.user_llm_cents_per_day

    def limit(self, kind: str) -> int:
        default = self.limits[kind]
        return self.overrides(kind, default) if self.overrides else default

    def effective_limits(self) -> dict[str, int]:
        return {kind: self.limit(kind) for kind in self.limits}

    def global_limit(self) -> int:
        return self.overrides("llm_global", self.global_llm_limit) if self.overrides else self.global_llm_limit

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
        limit = self.limit(kind)
        if not self._increment(f"usage/{self._day()}/{owner}", kind, amount, limit):
            raise HTTPException(429, f"Has llegado al límite diario de {KIND_LABELS[kind]} ({limit}). Vuelve mañana.")

    def cents_limit(self) -> int:
        default = self.global_cents_limit
        return self.overrides("ia_centimos_dia", default) if self.overrides else default

    def user_cents_limit_now(self) -> int:
        default = self.user_cents_limit
        return self.overrides("ia_centimos_usuario", default) if self.overrides else default

    def spent_micro_usd(self, who: str = "global") -> int:
        found = self.objects.read(f"usage/{self._day()}/{who}")
        return int(json.loads(found[0]).get("micro_usd", 0)) if found else 0

    def check_budget(self, owner: str | None = None) -> None:
        """Antes de llamar a un modelo: si hoy ya se ha gastado el presupuesto de IA (global o de esa persona)."""
        if self.spent_micro_usd() >= self.cents_limit() * 10_000:
            raise HTTPException(503, "La demo ha agotado su presupuesto diario de IA. Vuelve mañana.")
        if owner and self.spent_micro_usd(owner) >= self.user_cents_limit_now() * 10_000:
            raise HTTPException(
                429,
                "Has llegado al gasto diario en IA que puedo pagar por persona. Prueba con un modelo más barato mañana.",
            )

    def add_cost(self, usd: float, owner: str | None = None) -> None:
        """Suma lo gastado en una llamada (en millonésimas de dólar). Nunca falla por el límite."""
        micro = int(round(max(0.0, usd) * 1_000_000))
        if micro:
            self._increment(f"usage/{self._day()}/global", "micro_usd", micro, 10**15)
            if owner:
                self._increment(f"usage/{self._day()}/{owner}", "micro_usd", micro, 10**15)

    def consume_llm_calls(self, calls: int) -> None:
        """Fusible de coste: llamadas al LLM de todos los usuarios en el día (UTC)."""
        if not self._increment(f"usage/{self._day()}/global", "llm_calls", calls, self.global_limit()):
            raise HTTPException(503, "La demo ha agotado su presupuesto diario de IA. Vuelve mañana.")
