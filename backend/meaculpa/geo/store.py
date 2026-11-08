"""Partidas, fotos vistas, cuotas y coste, todo sobre el `ObjectStore` de Mea culpa (Blob en Azure).

- Partidas: `geo/<owner>/<id>`; fotos ya vistas: `geo/<owner>/vistas`.
- Cuotas: los mismos contadores diarios de Mea culpa (`usage/<día>/<owner>`), con campos propios del juego,
  y el fusible global compartido de toda la demo (`usage/<día>/global`, campo `llm_calls`) más uno del juego.
- Coste: `usage/<día>/geo_costes`, acumulado por modelo.
- Partida en curso de cada usuario (para retomarla al recargar): `geo/<owner>/actual`.
- Topes propios frente a Google Maps Platform: `usage/<día de California>/google` (partidas con Street View,
  capturas de Street View Static y cargas de mapa). Google reinicia sus cuotas diarias a medianoche de California.
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
from .config import MAPS_HARD_CAP, SV_PANORAMAS_HARD_CAP, SV_STATIC_HARD_CAP, GeoSettings
from .streetview import pacific_day

GEO_KIND_LABELS = {
    "geo_partidas": "partidas de ¿Dónde está?",
    "geo_ia": "rondas analizadas por la IA",
    "geo_sv_partidas": "partidas con Street View",
    "geo_recargas": "recargas de una partida a medias",
}
FUENTES = ("streetview", "fotos")
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
    fotos: list[str]  # partidas con fotos de Commons: ids del manifest
    rondas: list[dict[str, Any]] = field(default_factory=list)  # una por ronda respondida (o en curso)
    estado: str = "en_curso"  # en_curso | terminada | abandonada
    fuente: str = "fotos"  # streetview | fotos (las partidas anteriores a Street View no llevan el campo)
    # Partidas con Street View: por ronda, {"pano": pano_id, "pais": código ISO propio, "heading": grados}. Solo el
    # pano_id es de Google (se puede guardar); las coordenadas no se guardan nunca.
    ubicaciones: list[dict[str, Any]] = field(default_factory=list)
    aviso: str | None = None  # por qué una partida va con fotos cuando se quería Street View
    # Capturas de Street View Static: las reservadas al crear la partida (rondas x capturas, ya contadas en el tope
    # del día) y las pedidas hasta ahora. Pasada la reserva (reintentos tras un fallo), cada captura se cuenta aparte.
    reserva: int = 0
    capturas: int = 0
    fuera_ranking: str | None = None  # por qué una partida terminada no entra en el ranking
    etag: str | None = field(default=None, repr=False, compare=False)

    @property
    def total(self) -> int:
        return len(self.ubicaciones) if self.fuente == "streetview" else len(self.fotos)

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
        data = json.loads(raw)
        game = cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
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

    def current(self, owner: str) -> str | None:
        return self.current_with_etag(owner)[0]

    def current_with_etag(self, owner: str) -> tuple[str | None, str | None]:
        found = self.objects.read(f"geo/{owner}/actual")
        return (found[0].decode() or None, found[1]) if found else (None, None)

    def claim_current(self, owner: str, game_id: str, etag: str | None) -> str | None:
        """Apunta a una partida nueva solo si el puntero sigue como se leyó (`etag`; None = que no exista).

        Devuelve el etag nuevo, o None si otra petición se ha adelantado: dos peticiones a la vez para empezar
        partida no pueden dejar dos partidas en curso."""
        return self.objects.write(f"geo/{owner}/actual", game_id.encode(), etag=etag)

    def restore_current(self, owner: str, previous: str | None, etag: str) -> None:
        """Deshace `claim_current` (la partida nueva no se ha podido crear), si nadie ha tocado el puntero."""
        key = f"geo/{owner}/actual"
        if previous:
            self.objects.write(key, previous.encode(), etag=etag)
        else:
            found = self.objects.read(key)
            if found and found[1] == etag:
                self.objects.delete(key)

    def set_current(self, owner: str, game_id: str) -> None:
        """Apunta a la partida en curso sin condiciones (tests)."""
        key = f"geo/{owner}/actual"
        for _ in range(5):
            found = self.objects.read(key)
            if self.objects.write(key, game_id.encode(), etag=found[1] if found else None) is not None:
                return

    def clear_current(self, owner: str, game_id: str) -> None:
        """Borra el puntero si sigue apuntando a esa partida."""
        if self.current(owner) == game_id:
            self.objects.delete(f"geo/{owner}/actual")

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

    def __init__(
        self, objects: ObjectStore, settings: Settings, geo: GeoSettings, clock=time.time, overrides=None
    ) -> None:
        super().__init__(objects, settings, clock=clock, overrides=overrides)
        self.limits = {
            "geo_partidas": geo.quota_games_per_day,
            "geo_ia": geo.quota_llm_per_day,
            "geo_sv_partidas": geo.quota_sv_games_per_day,
            "geo_recargas": geo.quota_reloads_per_day,
        }
        self.geo_global_limit = geo.global_llm_per_day
        self.geo = geo

    def consume(self, owner: str, kind: str, amount: int = 1) -> None:
        limit = self.limit(kind)
        if not self._increment(f"usage/{self._day()}/{owner}", kind, amount, limit):
            raise HTTPException(
                429, f"Has llegado al límite diario de {GEO_KIND_LABELS[kind]} ({limit}). Vuelve mañana."
            )

    def consume_geo_llm(self, owner: str) -> None:
        """Una llamada a la IA: cuota del usuario, fusible del juego y fusible global de la demo.

        Si un fusible corta, se devuelve lo ya descontado: la ronda se reabre y no cuesta nada."""
        self.consume(owner, "geo_ia")
        if not self._increment(f"usage/{self._day()}/global", "geo_llm_calls", 1, self.geo_limit()):
            self.refund(owner, "geo_ia")
            raise HTTPException(503, "El juego ha agotado su presupuesto diario de IA. Vuelve mañana.")
        try:
            self.consume_llm_calls(1)
        except HTTPException:
            self._increment(f"usage/{self._day()}/global", "geo_llm_calls", -1, 10**9)
            self.refund(owner, "geo_ia")
            raise

    def release_geo_llm(self, owner: str) -> None:
        """Devuelve una llamada a la IA que al final no se ha hecho (por ejemplo, Google no dio las capturas)."""
        self.refund(owner, "geo_ia")
        self._increment(f"usage/{self._day()}/global", "geo_llm_calls", -1, 10**9)
        self._increment(f"usage/{self._day()}/global", "llm_calls", -1, 10**9)

    def try_consume(self, owner: str, kind: str, amount: int = 1) -> bool:
        """Como `consume`, pero devuelve False en vez de lanzar 429 (cuota de Street View: se juega con fotos)."""
        return self._increment(f"usage/{self._day()}/{owner}", kind, amount, self.limit(kind))

    # ---- topes frente a Google (día de California) --------------------------------------------------------
    def _google_key(self) -> str:
        return f"usage/{pacific_day(self.clock())}/google"

    def sv_games_limit(self) -> int:
        default = self.geo.sv_games_per_day
        value = self.overrides("geo_sv_global", default) if self.overrides else default
        return min(value, default)  # nunca por encima de lo que cabe en el tope de capturas

    def maps_limit(self) -> int:
        default = self.geo.maps_loads_per_day
        value = self.overrides("geo_mapas", default) if self.overrides else default
        return min(value, MAPS_HARD_CAP)  # lo que falta hasta el techo queda para recargas

    def panoramas_limit(self) -> int:
        default = self.geo.sv_panoramas_per_day
        value = self.overrides("geo_panoramas", default) if self.overrides else default
        return min(value, SV_PANORAMAS_HARD_CAP)

    def sv_static_limit(self) -> int:
        return min(self.geo.sv_static_per_day, SV_STATIC_HARD_CAP)

    def sv_reserve(self) -> int:
        """Capturas que reserva cada partida con Street View: todas las de sus rondas."""
        return self.geo.rounds * self.geo.sv_images

    def try_sv_game(self) -> bool:
        """Una partida con Street View más hoy, si cabe: reserva las capturas de todas sus rondas y su panorámica
        (que cuenta en su SKU y en la cuota de cargas de mapa).

        Las reservas de partidas nuevas solo llegan hasta el tope menos la holgura (`sv_static_slack`), que queda
        para reintentos; así una partida empezada no se queda a medias por el cupo de capturas."""
        key, reserve = self._google_key(), self.sv_reserve()
        if not self._increment(key, "sv_partidas", 1, self.sv_games_limit()):
            return False
        if not self._increment(key, "sv_imagenes", reserve, max(0, self.sv_static_limit() - self.geo.sv_static_slack)):
            self._increment(key, "sv_partidas", -1, 10**9)
            return False
        if not self.try_panoramas(1):
            self._increment(key, "sv_imagenes", -reserve, 10**9)
            self._increment(key, "sv_partidas", -1, 10**9)
            return False
        if not self.try_map_loads(1):
            self._increment(key, "panoramas", -1, 10**9)
            self._increment(key, "sv_imagenes", -reserve, 10**9)
            self._increment(key, "sv_partidas", -1, 10**9)
            return False
        return True

    def release_sv_game(self) -> None:
        """Devuelve la partida reservada (capturas y panorámica) si al final no se ha podido jugar con Street View."""
        key = self._google_key()
        self._increment(key, "sv_partidas", -1, 10**9)
        self._increment(key, "sv_imagenes", -self.sv_reserve(), 10**9)
        self._increment(key, "panoramas", -1, 10**9)
        self._increment(key, "mapas", -1, 10**9)

    def try_panoramas(self, n: int) -> bool:
        """Panorámicas de Street View dinámico: cada `new StreetViewPanorama` del navegador se cobra aparte."""
        return self._increment(self._google_key(), "panoramas", n, self.panoramas_limit())

    def release_panoramas(self, n: int) -> None:
        self._increment(self._google_key(), "panoramas", -n, 10**9)

    def try_sv_images(self, n: int) -> bool:
        """Capturas fuera de la reserva de su partida (reintentos). Techo duro por debajo de la cuota de Google."""
        return self._increment(self._google_key(), "sv_imagenes", n, self.sv_static_limit())

    def release_sv_images(self, n: int) -> None:
        self._increment(self._google_key(), "sv_imagenes", -n, 10**9)

    def try_map_loads(self, n: int) -> bool:
        """Cargas para una partida nueva: paran en el límite del día (/admin «geo_mapas»)."""
        return self._increment(self._google_key(), "mapas", n, self.maps_limit())

    def try_reload_loads(self, n: int) -> bool:
        """Cargas de una recarga de la página: pueden usar el margen hasta el techo, para que una partida empezada
        se pueda retomar aunque ya no quepan partidas nuevas."""
        return self._increment(self._google_key(), "mapas", n, MAPS_HARD_CAP)

    def add_map_loads(self, n: int) -> None:
        """Cuenta sin cortar o, con `n` negativo, devuelve una carga no usada."""
        self._increment(self._google_key(), "mapas", n, 10**9)

    def google_today(self) -> dict[str, int]:
        found = self.objects.read(self._google_key())
        counts = json.loads(found[0]) if found else {}
        return {k: int(counts.get(k, 0)) for k in ("sv_partidas", "sv_imagenes", "mapas", "panoramas")}

    def geo_limit(self) -> int:
        return self.overrides("geo_global", self.geo_global_limit) if self.overrides else self.geo_global_limit

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
