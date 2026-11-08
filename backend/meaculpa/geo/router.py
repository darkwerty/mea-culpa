"""Router FastAPI de «¿Dónde está?» bajo /api/geo.

Se monta en la API de Mea culpa con su login (dependencia `current_user`), su almacén y sus proveedores:

    from .geo import create_geo_router
    app.include_router(create_geo_router(current_user=current_user, objects=objects, settings=settings, models=models))

Dos fuentes de imágenes:
- Street View (por defecto): cada ronda es una panorámica oficial de Google. El navegador solo recibe su pano_id
  (lo necesita Google para pintarla con la Maps JavaScript API) y la orientación inicial; nunca coordenadas ni país.
  La IA recibe varias capturas de Street View Static de esa misma panorámica, en memoria y sin guardarlas.
- Fotos de Wikimedia Commons (respaldo): cuando Street View no está disponible (cupo del día agotado, clave sin
  configurar o fallo de Google) o la persona ya ha jugado hoy sus partidas con Street View. La partida lo dice.

Flujo de una ronda (el servidor guarda la solución, o solo el pano_id, y la revela tras la jugada):
1. GET  /partidas/actual             la partida en curso del usuario, para retomarla al recargar en la misma ronda.
2. GET  /partidas/{id}/foto/{n}      con fotos: imagen de la ronda n (solo la actual o anteriores; sin id de foto).
3. POST /partidas/{id}/rondas/{n}    marca del jugador. Se guarda primero (con etag: no se puede responder
   dos veces ni repetir la ronda), luego la IA recibe solo las imágenes, y se devuelven distancias, puntos y
   la solución.
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field

from ..auth import User
from ..config import Settings
from ..datos import DataStore
from ..llm.base import LLMProvider, cost_usd
from ..models import CATALOG_BY_ID, MODEL_CATALOG, build_model_providers, catalog_row, rival_label
from ..partidas import geo_row
from ..quota import UsageLimiter
from ..ranking import RankingStore
from ..storage import ObjectStore
from .config import GEO_DESCRIPTIONS, SV_STATIC_HARD_CAP, VISION_MODEL_IDS, GeoSettings, get_geo_settings
from .photos import PhotoSet
from .schemas import GuessIn
from .scoring import haversine_km, score
from .store import GeoConflictError, GeoGame, GeoLimiter, GeoStore, new_game_id
from .streetview import Countries, PanoPool, StreetViewClient, StreetViewError, fetch_images
from .vision import MockGeoProvider, locate

log = logging.getLogger("meaculpa.geo")

GEO_PREFIX = "/api/geo"
MAX_ATTEMPTS = 2  # un reintento si la salida no cumple el esquema
PENDING_TIMEOUT_S = 180  # una ronda con la IA «pensando» más de esto se cierra con cero puntos para la IA
MAP_LOADS = {"streetview": 2, "fotos": 1}  # mapa y panorámica, o solo mapa

AVISO_SIN_CLAVE = "Street View no está disponible ahora mismo: esta partida usa fotos de Wikimedia Commons."
AVISO_CUPO = "Cupo de Street View de hoy agotado: esta partida usa fotos de Wikimedia Commons. Mañana vuelve."
AVISO_FALLO = "Google Street View no responde ahora mismo: esta partida usa fotos de Wikimedia Commons."
SV_NO_RESPONDE = "Google Street View no responde ahora mismo. Prueba otra vez en unos segundos."


def aviso_persona(n: int) -> str:
    if n <= 0:
        return "Hoy no hay partidas con Street View para ti: esta usa fotos de Wikimedia Commons."
    partidas = "partida" if n == 1 else "partidas"
    return f"Ya has jugado hoy tus {n} {partidas} con Street View: esta usa fotos de Wikimedia Commons."


class NewGeoGame(BaseModel):
    modelo: str | None = Field(None, max_length=40)
    ranking: bool = True  # casilla «Salir en el ranking con mi nombre» (marcada por defecto)
    abandonar: bool = False  # con una partida a medias: True la da por abandonada (sin ranking) y empieza otra


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


def geo_quota_defaults(geo: GeoSettings) -> dict[str, int]:
    """Cuotas del juego que se pueden cambiar desde /admin, con su valor por defecto."""
    return {
        "geo_partidas": geo.quota_games_per_day,
        "geo_ia": geo.quota_llm_per_day,
        "geo_global": geo.global_llm_per_day,
        "geo_sv_partidas": geo.quota_sv_games_per_day,
        "geo_sv_global": geo.sv_games_per_day,
        "geo_mapas": geo.maps_loads_per_day,
    }


def create_geo_router(
    *,
    current_user: Callable[..., User],
    objects: ObjectStore,
    settings: Settings,
    models: dict[str, LLMProvider] | None = None,
    geo_settings: GeoSettings | None = None,
    photos: PhotoSet | None = None,
    data: DataStore | None = None,
    ranking: RankingStore | None = None,
    budget: UsageLimiter | None = None,
    pool: PanoPool | None = None,
) -> APIRouter:
    """`data`: registro de uso y controles de /admin (modelos desactivados, cuotas). Opcional en tests.

    `ranking`: rankings públicos (se guarda cada partida terminada). `budget`: fusible diario de gasto en IA
    compartido con el ajedrez (por defecto, el del propio limitador del juego). `pool`: reserva de pano_id (por
    defecto, la del almacén si hay clave de Street View).
    """
    geo = geo_settings or get_geo_settings()
    photos = photos or PhotoSet(geo.photos_dir)
    if len(photos) < geo.rounds:
        log.warning("Solo hay %d fotos en %s: el respaldo con fotos no funciona", len(photos), geo.photos_dir)
    models = vision_models(models, settings, geo)
    default_model = geo.default_model if geo.default_model in models else next(iter(models), None)
    store = GeoStore(objects)
    limiter = GeoLimiter(objects, settings, geo, overrides=data.quota if data else None)
    budget = budget or limiter
    router = APIRouter(prefix=GEO_PREFIX)
    countries = Countries()
    sv = StreetViewClient(geo.sv_key, geo.http_get) if geo.sv_key else None
    if sv is not None and pool is None:
        pool = PanoPool(objects, sv, geo, countries)
    if pool is not None and geo.sv_pool_background:
        pool.ensure()  # al arrancar la réplica, la reserva se rellena en segundo plano
    rng = random.SystemRandom()

    def _enabled(model_id: str) -> bool:
        return data.model_enabled(model_id) if data else True

    def _record(user: User, tipo: str, **kw: Any) -> None:
        if data is None:
            return
        try:
            data.record(user.owner, "geo", tipo, **kw)
        except Exception:  # noqa: BLE001 - el registro de uso nunca rompe la partida
            log.exception("No se pudo registrar el evento %s", tipo)

    def _save(game: GeoGame) -> None:
        try:
            store.save(game)
        except GeoConflictError as exc:
            raise HTTPException(409, "Esta ronda ya se ha respondido; recarga la partida") from exc
        if data is not None:
            try:
                data.save_game(geo_row(game, time.time(), rival_label(game.modelo)))  # copia en SQL para /admin
            except Exception:  # noqa: BLE001 - nunca rompe la partida
                log.exception("No se pudo registrar la partida %s", game.id)

    def _pending(game: GeoGame) -> bool:
        return bool(game.rondas) and game.rondas[-1].get("ia") is None

    def _finish_if_done(user: User, game: GeoGame) -> None:
        if game.estado == "en_curso" and game.ronda_actual >= game.total and not _pending(game):
            game.estado = "terminada"
            store.clear_current(user.owner, game.id)

    def _close_stale(user: User, game: GeoGame) -> GeoGame:
        """Si la IA se quedó colgada (la réplica se apagó a media ronda), la ronda se cierra con 0 para la IA."""
        if not _pending(game) or time.time() - float(game.rondas[-1].get("t", 0)) < PENDING_TIMEOUT_S:
            return game
        game.rondas[-1].update(ia={"error": "no ha podido responder", "distancia_km": None, "puntos": 0}, coste_usd=0.0)
        _finish_if_done(user, game)
        try:
            _save(game)
        except HTTPException:
            return store.get(user.owner, game.id) or game
        if game.estado == "terminada":
            _rank(user, game)
        return game

    def _get(user: User, game_id: str) -> GeoGame:
        game = store.get(user.owner, game_id)  # solo entre las partidas del propio usuario
        if game is None:
            raise HTTPException(404, "Partida no encontrada")
        return _close_stale(user, game)

    def _solutions(game: GeoGame, known: dict[int, tuple[float, float]] | None = None) -> dict[int, Any]:
        """Solución de las rondas ya jugadas. Con Street View, las coordenadas se piden a Google en el momento (los
        metadatos son gratis) y no se guardan: las condiciones solo permiten guardar el pano_id."""
        done = [i for i, r in enumerate(game.rondas) if r.get("ia") is not None]
        if game.fuente != "streetview":
            return {i: photos.get(game.fotos[i]).solution() for i in done}
        found: dict[int, tuple[float, float] | None] = dict(known or {})
        missing = [i for i in done if i not in found]

        def where(i: int) -> tuple[float, float] | None:
            try:
                return sv.location(game.ubicaciones[i]["pano"]) if sv else None
            except StreetViewError as exc:
                log.warning("Sin metadatos de la ronda %d de %s: %s", i, game.id, exc)
                return None

        if missing:
            with ThreadPoolExecutor(max_workers=min(5, len(missing))) as ex:
                found.update(zip(missing, ex.map(where, missing), strict=True))
        out = {}
        for i in done:
            loc = found.get(i)
            out[i] = {
                "pais": countries.name(game.ubicaciones[i]["pais"]),
                "region": None,
                "lat": round(loc[0], 6) if loc else None,
                "lon": round(loc[1], 6) if loc else None,
            }
        return out

    def _state(game: GeoGame, known: dict[int, tuple[float, float]] | None = None) -> dict[str, Any]:
        """Lo que ve el navegador: de la ronda en curso, solo la URL de su foto o el pano_id de su panorámica."""
        total = game.total
        solutions = _solutions(game, known)
        rondas = []
        for i, r in enumerate(game.rondas):
            if r.get("ia") is None:  # respondida pero la IA aún no ha contestado: se oculta todo
                continue
            row: dict[str, Any] = {
                "n": i,
                "jugador": r["jugador"],
                "ia": r["ia"],
                "solucion": solutions[i],
                "coste_usd": r.get("coste_usd", 0.0),
            }
            if game.fuente == "streetview":
                row["pano"] = game.ubicaciones[i]["pano"]
            else:
                row["atribucion"] = photos.get(game.fotos[i]).attribution()
                row["foto"] = f"{GEO_PREFIX}/partidas/{game.id}/foto/{i}"
            rondas.append(row)
        actual = game.ronda_actual
        playing = game.estado == "en_curso" and actual < total and not _pending(game)
        current = None
        if playing and game.fuente == "streetview":
            u = game.ubicaciones[actual]
            current = {"pano": u["pano"], "heading": u.get("heading", 0)}  # el pano_id lo necesita Google
        return {
            "id": game.id,
            "modelo": {"id": game.modelo, "etiqueta": _model_label(game.modelo, models.get(game.modelo))},
            "estado": game.estado,
            "fuente": game.fuente,
            "aviso": game.aviso,
            "total_rondas": total,
            "ronda_actual": actual,
            "pendiente": _pending(game),
            "foto_actual": f"{GEO_PREFIX}/partidas/{game.id}/foto/{actual}"
            if playing and game.fuente == "fotos"
            else None,
            "ubicacion_actual": current,
            "rondas": rondas,
            "marcador": {
                "jugador": sum(r["jugador"]["puntos"] for r in rondas),
                "ia": sum(r["ia"]["puntos"] for r in rondas),
            },
            "coste_total_usd": round(sum(r["coste_usd"] for r in rondas), 6),
        }

    def _ask_model(provider: LLMProvider, images: bytes | list[bytes]) -> tuple[dict[str, Any], float, dict[str, Any]]:
        """Llama a la IA (con un reintento si la salida no vale). Devuelve (resultado, coste, métricas)."""
        usd, tin, tout, ms, error = 0.0, 0, 0, 0.0, None
        for _ in range(MAX_ATTEMPTS):
            try:
                result = locate(provider, images, detail=geo.image_detail)
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
                **catalog_row(m, m.id in models and _enabled(m.id), default_model),
                "descripcion": GEO_DESCRIPTIONS.get(m.id, m.descripcion),
            }
            for m in MODEL_CATALOG
            if m.id in VISION_MODEL_IDS and (not m.oculto or m.id in models)
        ]
        for model_id, provider in models.items():
            if model_id not in CATALOG_BY_ID:
                rows.append(
                    {
                        "id": model_id,
                        "etiqueta": provider.name,
                        "descripcion": "Rival de pruebas, no mira la foto",
                        "proveedor": "local",
                        "fabricante": "local",
                        "razona": False,
                        "vision": True,
                        "disponible": True,
                        "por_defecto": model_id == default_model,
                    }
                )
        return {
            "modelos": rows,
            "por_defecto": default_model,
            "rondas": geo.rounds,
            "demo_activa": data.demo_enabled("geo") if data else True,
            "streetview": sv is not None,
        }

    def _streetview_rounds(user: User) -> tuple[list[dict[str, Any]] | None, str | None]:
        """Ubicaciones de Street View para una partida nueva, o (None, aviso) si toca jugar con fotos."""
        if sv is None or pool is None:
            return None, AVISO_SIN_CLAVE
        if not limiter.try_consume(user.owner, "geo_sv_partidas"):
            return None, aviso_persona(limiter.limit("geo_sv_partidas"))
        if not limiter.try_sv_game():
            limiter.try_consume(user.owner, "geo_sv_partidas", -1)  # se devuelve la de la persona
            return None, AVISO_CUPO
        entries: list[dict[str, Any]] = []
        try:
            if not geo.sv_pool_background:
                pool.ensure()  # en tests, el relleno va en la misma llamada
            entries = pool.take(geo.rounds)
            if entries and not pool.still_valid(entries):
                entries = pool.take(geo.rounds)  # alguna ya no existe: otra tanda
        except Exception as exc:  # noqa: BLE001 - cualquier fallo de Google o del almacén: partida con fotos
            log.warning("No se pudo sacar la reserva de Street View: %s", exc)
            entries = []
        finally:
            pool.ensure()
        if len(entries) < geo.rounds:
            limiter.try_consume(user.owner, "geo_sv_partidas", -1)
            limiter.release_sv_game()
            return None, AVISO_FALLO
        return [{"pano": e["pano"], "pais": e["pais"], "heading": round(rng.uniform(0, 360), 1)} for e in entries], None

    @router.post("/partidas")
    def new_game(req: NewGeoGame, user: User = Depends(current_user)) -> dict[str, Any]:
        model_id = req.modelo or default_model
        if model_id is None or model_id not in models or not _enabled(model_id):
            raise HTTPException(400, "Ese modelo no está disponible ahora mismo")
        # Partida a medias: se retoma en la misma ronda (recargar o volver a empezar no sirve para saltarse una).
        current_id = store.current(user.owner)
        current = store.get(user.owner, current_id) if current_id else None
        if current is not None and current.estado == "en_curso" and not req.abandonar:
            return {**_state(_close_stale(user, current)), "reanudada": True}
        if len(photos) < geo.rounds and sv is None:
            raise HTTPException(503, "El juego no tiene fotos cargadas")
        if not limiter.try_map_loads(1):
            raise HTTPException(503, "El mapa del juego ha llegado a su límite de hoy. Vuelve mañana.")
        limiter.consume(user.owner, "geo_partidas")  # sin cuota, la partida a medias sigue como estaba
        if current is not None and current.estado == "en_curso":
            current.estado = "abandonada"  # no cuenta para el ranking
            _save(current)
            store.clear_current(user.owner, current.id)
        ubicaciones, aviso = _streetview_rounds(user)
        if ubicaciones is not None:
            limiter.add_map_loads(1)  # la panorámica cuenta aparte del mapa
            game = GeoGame(
                id=new_game_id(),
                owner=user.owner,
                created=time.time(),
                modelo=model_id,
                fotos=[],
                fuente="streetview",
                ubicaciones=ubicaciones,
            )
        else:
            if len(photos) < geo.rounds:
                raise HTTPException(503, "El juego no tiene fotos cargadas")
            chosen = photos.pick(geo.rounds, store.seen(user.owner))
            game = GeoGame(
                id=new_game_id(),
                owner=user.owner,
                created=time.time(),
                modelo=model_id,
                fotos=chosen,
                fuente="fotos",
                aviso=aviso,
            )
        _save(game)
        store.set_current(user.owner, game.id)
        if game.fuente == "fotos":
            store.mark_seen(user.owner, game.fotos)
        _record(user, "geo_partida", ref=game.id, modelo=model_id)
        if ranking is not None:
            ranking.set_preference(user, req.ranking)
        return _state(game)

    @router.get("/partidas/actual")
    def current_game(user: User = Depends(current_user)) -> dict[str, Any]:
        """La partida en curso (o None), para retomarla al recargar la página en la misma ronda."""
        game_id = store.current(user.owner)
        game = store.get(user.owner, game_id) if game_id else None
        if game is None:
            return {"partida": None}
        game = _close_stale(user, game)
        if game.estado != "en_curso":
            store.clear_current(user.owner, game.id)
            return {"partida": None}
        limiter.add_map_loads(MAP_LOADS.get(game.fuente, 1))  # al recargar, el navegador vuelve a cargar el mapa
        return {"partida": _state(game)}

    @router.get("/partidas/{game_id}")
    def get_game(game_id: str, user: User = Depends(current_user)) -> dict[str, Any]:
        return _state(_get(user, game_id))

    @router.get("/partidas/{game_id}/foto/{n}")
    def photo(game_id: str, n: int, user: User = Depends(current_user)) -> Response:
        game = _get(user, game_id)
        if game.fuente != "fotos" or n < 0 or n >= len(game.fotos) or n > game.ronda_actual:
            raise HTTPException(404, "Esa foto aún no está disponible")
        return Response(
            photos.image_bytes(game.fotos[n]),
            media_type="image/jpeg",
            headers={"Cache-Control": "private, max-age=3600"},
        )

    def _round_target(game: GeoGame, n: int) -> tuple[float, float, bytes | list[bytes]]:
        """Solución e imágenes de la ronda `n`. Con Street View: coordenadas y capturas pedidas ahora, en memoria."""
        if game.fuente != "streetview":
            photo = photos.get(game.fotos[n])
            return photo.lat, photo.lon, photos.image_bytes(photo.id)
        if sv is None:
            raise HTTPException(503, "Street View no está disponible ahora mismo. Prueba otra vez más tarde.")
        u = game.ubicaciones[n]
        try:
            lat, lon = sv.location(u["pano"])
        except StreetViewError as exc:
            log.warning("Sin metadatos para la ronda %d de %s: %s", n, game.id, exc)
            raise HTTPException(503, SV_NO_RESPONDE) from None
        if not limiter.try_sv_images(geo.sv_images):
            raise HTTPException(503, "Cupo de Street View de hoy agotado: esta ronda se podrá terminar mañana.")
        try:
            images = fetch_images(sv, u["pano"], float(u.get("heading", 0)), geo)
        except StreetViewError as exc:
            log.warning("Sin capturas para la ronda %d de %s: %s", n, game.id, exc)
            raise HTTPException(503, SV_NO_RESPONDE) from None
        return lat, lon, images

    @router.post("/partidas/{game_id}/rondas/{n}")
    def answer(game_id: str, n: int, guess: GuessIn, user: User = Depends(current_user)) -> dict[str, Any]:
        game = _get(user, game_id)
        if game.estado != "en_curso":
            raise HTTPException(409, "La partida ya ha terminado")
        if _pending(game):
            raise HTTPException(409, "La IA todavía está respondiendo la ronda anterior")
        if n != game.ronda_actual:
            raise HTTPException(409, "Esa ronda ya se ha jugado o todavía no toca")
        provider = models.get(game.modelo)
        if provider is None or not _enabled(game.modelo):
            raise HTTPException(503, "El modelo de esta partida ya no está disponible")
        # Cuota antes de nada: si no hay cuota, la ronda sigue abierta para cuando la haya.
        budget.check_budget(user.owner)
        limiter.consume_geo_llm(user.owner)

        lat, lon, images = _round_target(game, n)
        d_user = haversine_km(guess.lat, guess.lon, lat, lon)
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
                "t": round(time.time(), 1),
            }
        )
        _save(game)

        # 2) La IA recibe solo las imágenes (con Street View, capturas en memoria que se sueltan al acabar).
        ai, usd, metrics = _ask_model(provider, images)
        del images
        budget.add_cost(usd, user.owner)
        if "lat" in ai:
            d_ai = haversine_km(ai["lat"], ai["lon"], lat, lon)
            ai.update(distancia_km=round(d_ai, 1), puntos=score(d_ai))
        else:
            ai.update(distancia_km=None, puntos=0)
        game.rondas[n].update(ia={**ai, **metrics}, coste_usd=round(usd, 6))
        _record(
            user,
            "geo_ronda",
            ref=game.id,
            modelo=game.modelo,
            tokens_in=metrics.get("tokens_entrada", 0),
            tokens_out=metrics.get("tokens_salida", 0),
            usd=usd,
        )
        _finish_if_done(user, game)
        _save(game)
        if game.estado == "terminada":
            _rank(user, game)
        state = _state(game, known={n: (lat, lon)})
        state["ultima"] = state["rondas"][-1]
        return state

    def _rank(user: User, game: GeoGame) -> None:
        """Partida terminada al ranking, con los puntos que ha calculado el servidor. Street View y fotos, aparte."""
        if ranking is None:
            return
        you = sum(r["jugador"]["puntos"] for r in game.rondas)
        ai = sum((r.get("ia") or {}).get("puntos", 0) for r in game.rondas)
        result = "victoria" if you > ai else "derrota" if you < ai else "tablas"
        try:
            ranking.record(
                "geo-sv" if game.fuente == "streetview" else "geo",
                game.id,
                user,
                rival=game.modelo,
                rival_name=rival_label(game.modelo),
                result=result,
                points=you,
                points_ai=ai,
            )
        except Exception:  # noqa: BLE001 - el ranking nunca rompe la partida
            log.exception("No se pudo guardar la partida %s en el ranking", game.id)

    @router.get("/costes")
    def costs(user: User = Depends(current_user)) -> dict[str, Any]:
        """Coste de hoy (UTC) por modelo y uso de Google Maps Platform (día de California), para vigilar el gasto."""
        return {
            "hoy": limiter.costs_today(),
            "limites_diarios": limiter.effective_limits(),
            "google_hoy": limiter.google_today(),
            "google_limites": {
                "sv_partidas": limiter.sv_games_limit(),
                "sv_imagenes": min(geo.sv_static_per_day, SV_STATIC_HARD_CAP),
                "mapas": limiter.maps_limit(),
            },
        }

    return router
