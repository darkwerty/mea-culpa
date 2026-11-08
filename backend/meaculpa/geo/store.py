"""Partidas, fotos vistas, cuotas y coste, todo sobre el `ObjectStore` de Mea culpa (Blob en Azure).

- Partidas: `geo/<owner>/<id>`; fotos ya vistas: `geo/<owner>/vistas`.
- Cuotas: los mismos contadores diarios de Mea culpa (`usage/<día>/<owner>`), con campos propios del juego,
  y el fusible global compartido de toda la demo (`usage/<día>/global`, campo `llm_calls`) más uno del juego.
- Coste: `usage/<día>/geo_costes`, acumulado por modelo.
"""

from __future__ import annotations

import json
import secrets
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from fastapi import HTTPException

from ..config import Settings
from ..quota import UsageLimiter
from ..storage import ObjectStore
from .config import GeoSettings

GEO_KIND_LABELS = {"geo_partidas": "partidas de ¿Dónde está esta foto?", "geo_ia": "fotos analizadas por la IA"}
MAX_SEEN = 400


class GeoConflictError(RuntimeError):
    pass


def new_game_id() -> str:
    return secrets.token_hex(8)


@dataclass
class GeoGame:
    id: str
    owner: str
    created: float
    modelo: str
    fotos: list[str]
    rondas: list[dict[str, Any]] = field(default_factory=list)  # una por ronda respondida (o en curso)
    estado: str = "en_curso"  # en_curso | terminada
    etag: str | None = field(default=None, repr=False, compare=False)

    @property
    def ronda_actual(self) -> int:
        """Índice de la primera ronda sin respuesta del jugador (== total si ha terminado)."""
        return len(self.rondas)

    def to_json(self) -> bytes:
        data = asdict(self)
        data.pop("etag", None)
        return json.dumps(data, ensure_ascii=False).encode()

    @classmethod
    def from_json(cls, raw: bytes, etag: str | None) -> GeoGame:
        game = cls(**json.loads(raw))
        game.etag = etag
        return game


class GeoStore:
    def __init__(self, objects: ObjectStore) -> None:
        self.objects = objects

    @staticmethod
    def _key(owner: str, game_id: str) -> str:
        return f"geo/{owner}/{game_id}"

    def get(self, owner: str, game_id: str) -> GeoGame | None:
        if not game_id.isalnum() or len(game_id) != 16:
            return None
        found = self.objects.read(self._key(owner, game_id))
        return GeoGame.from_json(*found) if found else None

    def save(self, game: GeoGame) -> None:
        """Escritura condicionada al etag leído: dos peticiones a la vez sobre la misma ronda no pasan las dos."""
        etag = self.objects.write(self._key(game.owner, game.id), game.to_json(), etag=game.etag)
        if etag is None:
            raise GeoConflictError(game.id)
        game.etag = etag

    def seen(self, owner: str) -> set[str]:
        found = self.objects.read(f"geo/{owner}/vistas")
        return set(json.loads(found[0])) if found else set()

    def mark_seen(self, owner: str, photo_ids: list[str]) -> None:
        key = f"geo/{owner}/vistas"
        for _ in range(5):
            found = self.objects.read(key)
            current, etag = (json.loads(found[0]), found[1]) if found else ([], None)
            merged = [p for p in current if p not in photo_ids] + photo_ids
            if self.objects.write(key, json.dumps(merged[-MAX_SEEN:]).encode(), etag=etag) is not None:
                return


class GeoLimiter(UsageLimiter):
    """El `UsageLimiter` de Mea culpa con los tipos de cuota del juego."""

    def __init__(self, objects: ObjectStore, settings: Settings, geo: GeoSettings, clock=time.time) -> None:
        super().__init__(objects, settings, clock=clock)
        self.limits = {"geo_partidas": geo.quota_games_per_day, "geo_ia": geo.quota_llm_per_day}
        self.geo_global_limit = geo.global_llm_per_day

    def consume(self, owner: str, kind: str, amount: int = 1) -> None:
        limit = self.limits[kind]
        if not self._increment(f"usage/{self._day()}/{owner}", kind, amount, limit):
            raise HTTPException(
                429, f"Has llegado al límite diario de {GEO_KIND_LABELS[kind]} ({limit}). Vuelve mañana."
            )

    def consume_geo_llm(self, owner: str) -> None:
        """Una llamada a la IA: cuota del usuario, fusible del juego y fusible global de la demo."""
        self.consume(owner, "geo_ia")
        if not self._increment(f"usage/{self._day()}/global", "geo_llm_calls", 1, self.geo_global_limit):
            raise HTTPException(503, "El juego ha agotado su presupuesto diario de IA. Vuelve mañana.")
        self.consume_llm_calls(1)

    def record_cost(self, model: str, input_tokens: int, output_tokens: int, usd: float) -> None:
        key = f"usage/{self._day()}/geo_costes"
        for _ in range(self.MAX_RETRIES):
            found = self.objects.read(key)
            data, etag = (json.loads(found[0]), found[1]) if found else ({}, None)
            row = data.setdefault(model, {"llamadas": 0, "tokens_entrada": 0, "tokens_salida": 0, "usd": 0.0})
            row["llamadas"] += 1
            row["tokens_entrada"] += int(input_tokens)
            row["tokens_salida"] += int(output_tokens)
            row["usd"] = round(row["usd"] + usd, 6)
            if self.objects.write(key, json.dumps(data).encode(), etag=etag) is not None:
                return

    def costs_today(self) -> dict[str, Any]:
        found = self.objects.read(f"usage/{self._day()}/geo_costes")
        return json.loads(found[0]) if found else {}
