"""Router FastAPI de «¿Dónde está esta foto?» bajo /api/geo.

Se monta en la API de Mea culpa con su login (dependencia `current_user`), su almacén y sus proveedores:

    from .geo import create_geo_router
    app.include_router(create_geo_router(current_user=current_user, objects=objects, settings=settings, models=models))

Flujo de una ronda (el servidor guarda la solución y solo la revela tras la jugada):
1. GET  /partidas/{id}/foto/{n}      imagen de la ronda n (solo la actual o anteriores; sin id de foto).
2. POST /partidas/{id}/rondas/{n}    marca del jugador. Se guarda primero (con etag: no se puede responder
   dos veces ni repetir la ronda), luego la IA recibe solo la imagen, y se devuelven distancias, puntos,
   la solución y la atribución de la foto.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field

from ..auth import User
from ..config import Settings
from ..llm.base import LLMProvider, cost_usd
from ..models import CATALOG_BY_ID, MODEL_CATALOG, build_model_providers
from ..storage import ObjectStore
from .config import GEO_DESCRIPTIONS, VISION_MODEL_IDS, GeoSettings, get_geo_settings
from .photos import PhotoSet
from .schemas import GuessIn
from .scoring import haversine_km, score
from .store import GeoConflictError, GeoGame, GeoLimiter, GeoStore, new_game_id
from .vision import MockGeoProvider, locate

log = logging.getLogger("meaculpa.geo")

GEO_PREFIX = "/api/geo"
MAX_ATTEMPTS = 2  # un reintento si la salida no cumple el esquema


class NewGeoGame(BaseModel):
    modelo: str | None = Field(None, max_length=40)


def vision_models(
    models: dict[str, LLMProvider] | None, settings: Settings, geo: GeoSettings
) -> dict[str, LLMProvider]:
    """Solo los modelos con visión. Sin ninguno y en modo mock, el rival de pruebas."""
    if models is None:
        models = build_model_providers(settings) if settings.llm_provider != "mock" else {}
    allowed = set(geo.models or VISION_MODEL_IDS) & set(VISION_MODEL_IDS)
    out = {k: v for k, v in models.items() if k in allowed or hasattr(v, "locate")}
    if not out and settings.llm_provider == "mock":
        mock = MockGeoProvider()
        out = {mock.name: mock}
    return out


def _model_label(model_id: str, provider: Any) -> str:
    spec = CATALOG_BY_ID.get(model_id)
    return (
        f"{spec.etiqueta}, {GEO_DESCRIPTIONS.get(spec.id, spec.descripcion).lower()}"
        if spec
        else getattr(provider, "name", model_id)
    )


def create_geo_router(
    *,
    current_user: Callable[..., User],
    objects: ObjectStore,
    settings: Settings,
    models: dict[str, LLMProvider] | None = None,
    geo_settings: GeoSettings | None = None,
    photos: PhotoSet | None = None,
) -> APIRouter:
    geo = geo_settings or get_geo_settings()
    photos = photos or PhotoSet(geo.photos_dir)
    if len(photos) < geo.rounds:
        log.warning("Solo hay %d fotos en %s: no se pueden empezar partidas", len(photos), geo.photos_dir)
    models = vision_models(models, settings, geo)
    default_model = geo.default_model if geo.default_model in models else next(iter(models), None)
    store = GeoStore(objects)
    limiter = GeoLimiter(objects, settings, geo)
    router = APIRouter(prefix=GEO_PREFIX)

    def _get(user: User, game_id: str) -> GeoGame:
        game = store.get(user.owner, game_id)  # solo entre las partidas del propio usuario
        if game is None:
            raise HTTPException(404, "Partida no encontrada")
        return game

    def _save(game: GeoGame) -> None:
        try:
            store.save(game)
        except GeoConflictError as exc:
            raise HTTPException(409, "Esta ronda ya se ha respondido; recarga la partida") from exc

    def _state(game: GeoGame) -> dict[str, Any]:
        """Lo que ve el navegador: nada de la ronda en curso salvo la URL de su imagen."""
        total = len(game.fotos)
        rondas = []
        for i, r in enumerate(game.rondas):
            if r.get("ia") is None:  # respondida pero la IA aún no ha contestado: se oculta todo
                continue
            photo = photos.get(game.fotos[i])
            rondas.append(
                {
                    "n": i,
                    "jugador": r["jugador"],
                    "ia": r["ia"],
                    "solucion": photo.solution(),
                    "atribucion": photo.attribution(),
                    "coste_usd": r.get("coste_usd", 0.0),
                    "foto": f"{GEO_PREFIX}/partidas/{game.id}/foto/{i}",
                }
            )
        actual = game.ronda_actual
        return {
            "id": game.id,
            "modelo": {"id": game.modelo, "etiqueta": _model_label(game.modelo, models.get(game.modelo))},
            "estado": game.estado,
            "total_rondas": total,
            "ronda_actual": actual,
            "foto_actual": f"{GEO_PREFIX}/partidas/{game.id}/foto/{actual}" if actual < total else None,
            "rondas": rondas,
            "marcador": {
                "jugador": sum(r["jugador"]["puntos"] for r in rondas),
                "ia": sum(r["ia"]["puntos"] for r in rondas),
            },
            "coste_total_usd": round(sum(r["coste_usd"] for r in rondas), 6),
        }

    def _ask_model(provider: LLMProvider, image: bytes) -> tuple[dict[str, Any], float, dict[str, Any]]:
        """Llama a la IA (con un reintento si la salida no vale). Devuelve (resultado, coste, métricas)."""
        usd, tin, tout, ms, error = 0.0, 0, 0, 0.0, None
        for _ in range(MAX_ATTEMPTS):
            try:
                result = locate(provider, image, detail=geo.image_detail)
            except Exception as exc:  # noqa: BLE001 - un fallo del proveedor no rompe la partida
                log.warning("Fallo del modelo %s: %s", getattr(provider, "name", "?"), exc)
                error = "no ha podido responder"
                break
            call_usd = cost_usd(provider, result.input_tokens, result.output_tokens)
            limiter.record_cost(getattr(provider, "name", "?"), result.input_tokens, result.output_tokens, call_usd)
            usd, tin, tout, ms = (
                usd + call_usd,
                tin + result.input_tokens,
                tout + result.output_tokens,
                ms + result.latency_ms,
            )
            if result.parsed is not None and not result.error:
                return result.parsed.model_dump(), usd, {"tokens_entrada": tin, "tokens_salida": tout, "ms": round(ms)}
            log.info("Salida no válida de %s: %s", getattr(provider, "name", "?"), result.error)
            error = "respuesta no válida"
        return {"error": error}, usd, {"tokens_entrada": tin, "tokens_salida": tout, "ms": round(ms)}

    @router.get("/models")
    def list_models() -> dict[str, Any]:
        """Público: solo los modelos que aceptan imágenes (disponible = desplegado y activado)."""
        rows = [
            {
                "id": m.id,
                "etiqueta": m.etiqueta,
                "descripcion": GEO_DESCRIPTIONS.get(m.id, m.descripcion),
                "proveedor": "Anthropic" if m.proveedor == "claude" else "OpenAI",
                "disponible": m.id in models,
                "por_defecto": m.id == default_model,
            }
            for m in MODEL_CATALOG
            if m.id in VISION_MODEL_IDS
        ]
        for model_id, provider in models.items():
            if model_id not in CATALOG_BY_ID:
                rows.append(
                    {
                        "id": model_id,
                        "etiqueta": provider.name,
                        "descripcion": "Rival de pruebas, no mira la foto",
                        "proveedor": "local",
                        "disponible": True,
                        "por_defecto": model_id == default_model,
                    }
                )
        return {"modelos": rows, "por_defecto": default_model, "rondas": geo.rounds}

    @router.post("/partidas")
    def new_game(req: NewGeoGame, user: User = Depends(current_user)) -> dict[str, Any]:
        model_id = req.modelo or default_model
        if model_id is None or model_id not in models:
            raise HTTPException(400, "Ese modelo no está disponible ahora mismo")
        if len(photos) < geo.rounds:
            raise HTTPException(503, "El juego no tiene fotos cargadas")
        limiter.consume(user.owner, "geo_partidas")
        chosen = photos.pick(geo.rounds, store.seen(user.owner))
        game = GeoGame(id=new_game_id(), owner=user.owner, created=time.time(), modelo=model_id, fotos=chosen)
        _save(game)
        store.mark_seen(user.owner, chosen)
        return _state(game)

    @router.get("/partidas/{game_id}")
    def get_game(game_id: str, user: User = Depends(current_user)) -> dict[str, Any]:
        return _state(_get(user, game_id))

    @router.get("/partidas/{game_id}/foto/{n}")
    def photo(game_id: str, n: int, user: User = Depends(current_user)) -> Response:
        game = _get(user, game_id)
        if n < 0 or n >= len(game.fotos) or n > game.ronda_actual:
            raise HTTPException(404, "Esa foto aún no está disponible")
        return Response(
            photos.image_bytes(game.fotos[n]),
            media_type="image/jpeg",
            headers={"Cache-Control": "private, max-age=3600"},
        )

    @router.post("/partidas/{game_id}/rondas/{n}")
    def answer(game_id: str, n: int, guess: GuessIn, user: User = Depends(current_user)) -> dict[str, Any]:
        game = _get(user, game_id)
        if game.estado != "en_curso":
            raise HTTPException(409, "La partida ya ha terminado")
        if n != game.ronda_actual:
            raise HTTPException(409, "Esa ronda ya se ha jugado o todavía no toca")
        provider = models.get(game.modelo)
        if provider is None:
            raise HTTPException(503, "El modelo de esta partida ya no está disponible")
        # Cuota antes de nada: si no hay cuota, la ronda sigue abierta para cuando la haya.
        limiter.consume_geo_llm(user.owner)

        photo = photos.get(game.fotos[n])
        d_user = haversine_km(guess.lat, guess.lon, photo.lat, photo.lon)
        # 1) Se fija la respuesta del jugador (escritura condicionada): ya no se puede cambiar ni repetir.
        game.rondas.append(
            {
                "jugador": {
                    "lat": guess.lat,
                    "lon": guess.lon,
                    "distancia_km": round(d_user, 1),
                    "puntos": score(d_user),
                },
                "ia": None,
            }
        )
        _save(game)

        # 2) La IA recibe solo la imagen.
        ai, usd, metrics = _ask_model(provider, photos.image_bytes(photo.id))
        if "lat" in ai:
            d_ai = haversine_km(ai["lat"], ai["lon"], photo.lat, photo.lon)
            ai.update(distancia_km=round(d_ai, 1), puntos=score(d_ai))
        else:
            ai.update(distancia_km=None, puntos=0)
        game.rondas[n].update(ia={**ai, **metrics}, coste_usd=round(usd, 6))
        if game.ronda_actual >= len(game.fotos):
            game.estado = "terminada"
        _save(game)
        state = _state(game)
        state["ultima"] = state["rondas"][-1]
        return state

    @router.get("/costes")
    def costs(user: User = Depends(current_user)) -> dict[str, Any]:
        """Coste acumulado de hoy (UTC) por modelo, para vigilar el gasto."""
        return {"hoy": limiter.costs_today(), "limites_diarios": limiter.limits}

    return router
