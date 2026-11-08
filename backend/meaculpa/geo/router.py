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
   dos veces ni repetir la ronda), y solo entonces se piden las capturas y la IA recibe las imágenes. Si algo
   falla antes de llamar a la IA, la ronda se reabre y se devuelve lo descontado.

Coste frente a Google: cada partida con Street View reserva al crearse sus capturas Static y su panorámica
dinámica. Recargar la página con una partida a medias crea otra panorámica y otro mapa en el navegador: por eso
las recargas tienen cuota por persona y día y gastan del tope de panorámicas; sin cupo, la partida se ve sin
panorámica ni mapa hasta el día siguiente (o se abandona).
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
from .config import GEO_DESCRIPTIONS, VISION_MODEL_IDS, GeoSettings, get_geo_settings
from .photos import PhotoSet
from .schemas import GuessIn
from .scoring import haversine_km, score
from .store import GeoConflictError, GeoGame, GeoLimiter, GeoStore, new_game_id
from .streetview import Countries, PanoPool, StreetViewClient, StreetViewError, fetch_images
from .vision import MockGeoProvider, locate

log = logging.getLogger("meaculpa.geo")

GEO_PREFIX = "/api/geo"
MAX_ATTEMPTS = 2  # un reintento si la salida no cumple el esquema
# Una ronda con la IA «pensando» más de esto se cierra con cero puntos para la IA. Por encima del peor caso real:
# MAX_ATTEMPTS x 120 s de tiempo máximo de Foundry x 2 (un reintento del SDK) = 480 s, más las capturas.
PENDING_TIMEOUT_S = 600
STALE_ERROR = "no ha podido responder"
MAP_LOADS = {"streetview": 2, "fotos": 1}  # al recargar: mapa y panorámica, o solo mapa
# Dos rondas o más a menos de 1 km en sitios al azar de todo el mundo no son creíbles sin mirar dónde está.
SUSPICIOUS_KM = 1.0
SUSPICIOUS_ROUNDS = 2
FUERA_RANKING = (
    "Esta partida no entra en el ranking: dos o más rondas a menos de 1 km no son creíbles en sitios al azar "
    "de todo el mundo."
)

AVISO_SIN_CLAVE = "Street View no está disponible ahora mismo: esta partida usa fotos de Wikimedia Commons."
AVISO_CUPO = "Cupo de Street View de hoy agotado: esta partida usa fotos de Wikimedia Commons. Mañana vuelve."
AVISO_FALLO = "Google Street View no responde ahora mismo: esta partida usa fotos de Wikimedia Commons."
SV_NO_RESPONDE = "Google Street View no responde ahora mismo. Prueba otra vez en unos segundos."
SV_CUPO_RONDA = "Cupo de Street View de hoy agotado: esta ronda se podrá terminar mañana."
BLOQUEO_RECARGAS = (
    "Has recargado la página muchas veces hoy con esta partida a medias, y cada recarga es una carga de pago en "
    "Google. Puedes seguirla mañana o abandonarla y empezar otra."
)
BLOQUEO_MAPAS = (
    "El mapa del juego ha llegado a su límite de hoy en Google: esta partida a medias se puede seguir mañana."
)
BLOQUEO_PANORAMAS = (
    "Cupo de Street View de hoy agotado: esta partida a medias se puede seguir mañana, o abandonarla y jugar "
    "con fotos de Wikimedia Commons."
)


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
        "geo_panoramas": geo.sv_panoramas_per_day,
        "geo_recargas": geo.quota_reloads_per_day,
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
            if game.fuente == "streetview":
                near = sum(1 for r in game.rondas if float(r["jugador"]["distancia_km"]) < SUSPICIOUS_KM)
                if near >= SUSPICIOUS_ROUNDS:
                    game.fuera_ranking = FUERA_RANKING
            store.clear_current(user.owner, game.id)

    def _close_stale(user: User, game: GeoGame) -> GeoGame:
        """Si la IA se quedó colgada (la réplica se apagó a media ronda), la ronda se cierra con 0 para la IA."""
        if not _pending(game) or time.time() - float(game.rondas[-1].get("t", 0)) < PENDING_TIMEOUT_S:
            return game
        game.rondas[-1].update(
            ia={"error": STALE_ERROR, "distancia_km": None, "puntos": 0}, coste_usd=0.0, caducada=True
        )
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

    def _state(
        game: GeoGame, known: dict[int, tuple[float, float]] | None = None, bloqueo: str | None = None
    ) -> dict[str, Any]:
        """Lo que ve el navegador: de la ronda en curso, solo la URL de su foto o el pano_id de su panorámica.

        Con `bloqueo` (sin cupo de recargas o de panorámicas), ni pano_id ni foto: el navegador no carga nada."""
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
        playing = game.estado == "en_curso" and actual < total and not _pending(game) and bloqueo is None
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
            "bloqueo": bloqueo,
            "fuera_ranking": game.fuera_ranking,
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
            _release_sv(user)
            return None, AVISO_FALLO
        return [{"pano": e["pano"], "pais": e["pais"], "heading": round(rng.uniform(0, 360), 1)} for e in entries], None

    def _release_sv(user: User) -> None:
        limiter.try_consume(user.owner, "geo_sv_partidas", -1)
        limiter.release_sv_game()

    def _reload(user: User, game: GeoGame) -> str | None:
        """Una recarga de la página con la partida a medias: el navegador vuelve a crear el mapa y, con Street View,
        la panorámica. Cuenta en la cuota de recargas de la persona y en el tope de panorámicas del día.
        Devuelve el motivo del bloqueo, o None si se puede pintar."""
        if game.estado != "en_curso":
            return None
        if not limiter.try_consume(user.owner, "geo_recargas"):
            return BLOQUEO_RECARGAS
        streetview = game.fuente == "streetview"
        if streetview and not limiter.try_panoramas(1):
            limiter.try_consume(user.owner, "geo_recargas", -1)
            return BLOQUEO_PANORAMAS
        if not limiter.try_reload_loads(MAP_LOADS.get(game.fuente, 1)):
            limiter.try_consume(user.owner, "geo_recargas", -1)
            if streetview:
                limiter.release_panoramas(1)
            return BLOQUEO_MAPAS
        return None

    @router.post("/partidas")
    def new_game(req: NewGeoGame, user: User = Depends(current_user)) -> dict[str, Any]:
        model_id = req.modelo or default_model
        if model_id is None or model_id not in models or not _enabled(model_id):
            raise HTTPException(400, "Ese modelo no está disponible ahora mismo")
        # Partida a medias: se retoma en la misma ronda (recargar o volver a empezar no sirve para saltarse una).
        current_id, pointer_etag = store.current_with_etag(user.owner)
        current = store.get(user.owner, current_id) if current_id else None
        for _ in range(12 if current_id and current is None else 0):
            time.sleep(0.25)  # otra petición la está creando ahora mismo: se espera a que se guarde
            current = store.get(user.owner, current_id)
            if current is not None:
                break
        if current is not None and current.estado == "en_curso" and not req.abandonar:
            return _resume(user, current)
        if len(photos) < geo.rounds and sv is None:
            raise HTTPException(503, "El juego no tiene fotos cargadas")
        # Primero la cuota de la persona (sin cuota no se toca ningún contador global), luego el mapa.
        limiter.consume(user.owner, "geo_partidas")
        if not limiter.try_map_loads(1):
            limiter.refund(user.owner, "geo_partidas")
            raise HTTPException(503, "El mapa del juego ha llegado a su límite de hoy. Vuelve mañana.")
        # El puntero a la partida en curso se cambia con escritura condicionada antes de reservar nada de Google:
        # si otra petición ha empezado partida a la vez, gana ella y esta devuelve lo descontado y la enseña.
        game_id = new_game_id()
        claimed = store.claim_current(user.owner, game_id, pointer_etag)
        if claimed is None:
            limiter.refund(user.owner, "geo_partidas")
            limiter.add_map_loads(-1)
            return _winner(user)
        ubicaciones: list[dict[str, Any]] | None = None
        try:
            ubicaciones, aviso = _streetview_rounds(user)
            if ubicaciones is not None:
                game = GeoGame(
                    id=game_id,
                    owner=user.owner,
                    created=time.time(),
                    modelo=model_id,
                    fotos=[],
                    fuente="streetview",
                    ubicaciones=ubicaciones,
                    reserva=limiter.sv_reserve(),
                )
            else:
                if len(photos) < geo.rounds:
                    raise HTTPException(503, "El juego no tiene fotos cargadas")
                game = GeoGame(
                    id=game_id,
                    owner=user.owner,
                    created=time.time(),
                    modelo=model_id,
                    fotos=photos.pick(geo.rounds, store.seen(user.owner)),
                    fuente="fotos",
                    aviso=aviso,
                )
            _save(game)
        except Exception:
            limiter.refund(user.owner, "geo_partidas")
            limiter.add_map_loads(-1)
            if ubicaciones is not None:
                _release_sv(user)
            store.restore_current(user.owner, current_id, claimed)  # la partida de antes sigue en curso
            raise
        if current is not None and current.estado == "en_curso":
            current.estado = "abandonada"  # no cuenta para el ranking
            try:
                _save(current)
            except HTTPException:
                log.warning("No se pudo marcar como abandonada la partida %s", current.id)
        if game.fuente == "fotos":
            store.mark_seen(user.owner, game.fotos)
        _record(user, "geo_partida", ref=game.id, modelo=model_id)
        if ranking is not None:
            ranking.set_preference(user, req.ranking)
        return _state(game)

    def _winner(user: User) -> dict[str, Any]:
        """La partida que ha empezado otra petición a la vez (puede tardar un momento en guardarse)."""
        for _ in range(20):
            winner_id = store.current(user.owner)
            winner = store.get(user.owner, winner_id) if winner_id else None
            if winner is not None and winner.estado == "en_curso":
                return {**_state(winner), "reanudada": True}
            time.sleep(0.25)
        raise HTTPException(409, "Ya estás empezando otra partida; recarga la página")

    def _resume(user: User, game: GeoGame) -> dict[str, Any]:
        game = _close_stale(user, game)
        if game.estado != "en_curso":
            store.clear_current(user.owner, game.id)
            return {**_state(game), "reanudada": True}
        return {**_state(game, bloqueo=_reload(user, game)), "reanudada": True}

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
        return {"partida": _state(game, bloqueo=_reload(user, game))}

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

    def _round_place(game: GeoGame, n: int) -> tuple[float, float]:
        """Solución de la ronda `n`. Con Street View, de los metadatos (gratis), en el momento y sin guardarla."""
        if game.fuente != "streetview":
            photo = photos.get(game.fotos[n])
            return photo.lat, photo.lon
        if sv is None:
            raise HTTPException(503, "Street View no está disponible ahora mismo. Prueba otra vez más tarde.")
        try:
            return sv.location(game.ubicaciones[n]["pano"])
        except StreetViewError as exc:
            log.warning("Sin metadatos para la ronda %d de %s: %s", n, game.id, exc)
            raise HTTPException(503, SV_NO_RESPONDE) from None

    def _round_images(game: GeoGame, n: int) -> bytes | list[bytes]:
        """Imágenes para la IA: la foto, o las capturas de Street View Static pedidas ahora, solo en memoria."""
        if game.fuente != "streetview":
            return photos.image_bytes(game.fotos[n])
        u = game.ubicaciones[n]
        try:
            return fetch_images(sv, u["pano"], float(u.get("heading", 0)), geo)
        except StreetViewError as exc:
            log.warning("Sin capturas para la ronda %d de %s: %s", n, game.id, exc)
            raise HTTPException(503, SV_NO_RESPONDE) from None

    def _reopen(game: GeoGame, n: int) -> None:
        """Quita la respuesta de la ronda `n` (algo falló antes de llamar a la IA): se puede volver a responder."""
        if len(game.rondas) == n + 1 and game.rondas[n].get("ia") is None:
            game.rondas.pop()
            try:
                store.save(game)
            except GeoConflictError:
                log.warning("No se pudo reabrir la ronda %d de %s", n, game.id)

    @router.post("/partidas/{game_id}/rondas/{n}")
    def answer(game_id: str, n: int, guess: GuessIn, user: User = Depends(current_user)) -> dict[str, Any]:
        game = _get(user, game_id)
        if game.estado != "en_curso":
            raise HTTPException(409, "La partida ya ha terminado")
        if _pending(game):
            raise HTTPException(409, "La IA todavía está respondiendo la ronda anterior")
        if n != game.ronda_actual:
            raise HTTPException(409, "Esa ronda ya se ha jugado o todavía no toca")
        if store.current(user.owner) != game.id:
            raise HTTPException(409, "Esta partida ya no es la que tienes en curso; recarga la página")
        provider = models.get(game.modelo)
        if provider is None or not _enabled(game.modelo):
            raise HTTPException(503, "El modelo de esta partida ya no está disponible")
        budget.check_budget(user.owner)  # solo mira: si no hay presupuesto, la ronda sigue abierta

        lat, lon = _round_place(game, n)  # metadatos gratis: aún no se ha pagado nada
        d_user = haversine_km(guess.lat, guess.lon, lat, lon)
        # 1) Se fija la respuesta del jugador (escritura condicionada) antes de gastar nada: de varias respuestas a
        #    la vez solo pasa una, y las demás reciben 409 sin pedir capturas ni llamar a la IA.
        need = geo.sv_images if game.fuente == "streetview" else 0
        extra = need if game.capturas + need > game.reserva else 0  # fuera de la reserva: reintento
        game.capturas += need
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

        # 2) Cuota de IA y capturas. Si algo falla aquí, la ronda se reabre y se devuelve lo descontado.
        llm_taken = False
        try:
            limiter.consume_geo_llm(user.owner)
            llm_taken = True
            if extra and not limiter.try_sv_images(extra):
                game.capturas -= need  # no se ha pedido nada
                raise HTTPException(503, SV_CUPO_RONDA)
            images = _round_images(game, n)
        except HTTPException:
            if llm_taken:
                limiter.release_geo_llm(user.owner)
            _reopen(game, n)
            raise

        # 3) La IA recibe solo las imágenes (con Street View, capturas en memoria que se sueltan al acabar).
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
        try:
            _save(game)
        except HTTPException:
            # Solo pasa si la ronda se cerró por caducada mientras la IA pensaba: se guarda la respuesta real.
            fresh = store.get(user.owner, game.id)
            if fresh is None or len(fresh.rondas) <= n or not fresh.rondas[n].get("caducada"):
                raise
            fresh.rondas[n] = game.rondas[n]
            fresh.fuera_ranking = fresh.fuera_ranking or game.fuera_ranking
            _save(fresh)
            game = fresh
        if game.estado == "terminada":
            _rank(user, game)
        state = _state(game, known={n: (lat, lon)})
        state["ultima"] = state["rondas"][-1]
        return state

    def _rank(user: User, game: GeoGame) -> None:
        """Partida terminada al ranking, con los puntos que ha calculado el servidor. Street View y fotos, aparte."""
        if ranking is None or game.fuera_ranking:
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
                "sv_imagenes": limiter.sv_static_limit(),
                "mapas": limiter.maps_limit(),
                "panoramas": limiter.panoramas_limit(),
            },
        }

    return router
