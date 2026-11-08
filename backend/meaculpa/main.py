"""API FastAPI de Mea culpa."""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from typing import Annotated, Any, Literal

import chess
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .admin import create_admin_router
from .analysis import (
    AnalysisTimeout,
    analyze_moves,
    diagnose,
    history_counts,
    make_analyzer,
    parse_pgn,
    pgn_without_comments,
)
from .auth import TokenVerifier, User, google_verifier, make_user_dependency
from .config import Settings, get_settings
from .contacto import create_contact_router
from .costes_azure import AzureCosts, http_fetcher
from .datos import DataStore
from .db import Database, mssql_connector
from .engines import classic_opponent_catalog, find_stockfish
from .games import (
    GameConflictError,
    GameRecord,
    GameStore,
    build_opponent,
    finish_if_over,
    game_state,
    new_game_id,
)
from .geo import GEO_PREFIX, create_geo_router
from .geo.config import GeoSettings, get_geo_settings
from .geo.photos import PhotoSet
from .llm import LLMProvider, get_provider
from .llm_player import MetricsStore
from .models import CATALOG_BY_ID, build_model_providers, model_catalog
from .quota import UsageLimiter
from .storage import ObjectStore, make_object_store

log = logging.getLogger("meaculpa")


def _setup_telemetry() -> None:
    """Application Insights si hay cadena de conexión y el paquete opcional está instalado."""
    if not os.environ.get("APPLICATIONINSIGHTS_CONNECTION_STRING"):
        return
    try:
        from azure.monitor.opentelemetry import configure_azure_monitor
    except ImportError:
        log.warning("APPLICATIONINSIGHTS_CONNECTION_STRING definido pero falta azure-monitor-opentelemetry")
        return
    configure_azure_monitor()


class OpponentConfig(BaseModel):
    type: Literal["random", "greedy", "minimax", "stockfish", "llm"]
    depth: int = Field(3, ge=1, le=4)
    skill: int = Field(5, ge=0, le=20)
    temperature: float | None = Field(None, ge=0.0, le=2.0)  # None = valor por defecto del proveedor
    show_legal_moves: bool = False
    model: str | None = Field(None, max_length=40)  # id del catálogo de modelos (solo para type="llm")


class NewGameRequest(BaseModel):
    opponent: OpponentConfig
    user_color: Literal["white", "black"] = "white"


class MoveRequest(BaseModel):
    uci: str = Field(..., min_length=4, max_length=5)


class PgnAnalysisRequest(BaseModel):
    pgn: str = Field(..., max_length=20_000)
    user_color: Literal["white", "black"]


# Todas las rutas cuelgan de aquí: la API se sirve en danielalonso.dev/api/meaculpa (backend enlazado a la SWA).
API_PREFIX = "/api/meaculpa"

OPPONENT_NAMES = {
    "random": "Aleatorio",
    "greedy": "Codicioso",
    "minimax": "Minimax",
    "stockfish": "Stockfish",
    "llm": "IA de Foundry",
}

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
    "Cache-Control": "no-store",
}


def current_user(request: Request) -> User:
    """Dependencia de FastAPI: verifica el ID token de Google, registra la visita y rechaza a los bloqueados."""
    user = request.app.state.authenticate(request)
    if request.app.state.data.touch_user(user.owner, user.email, user.name):
        raise HTTPException(403, "Tu cuenta no puede usar las demos. Escribe a hola@danielalonso.dev si es un error.")
    return user


def _azure_credential(settings: Settings) -> Any:
    if settings.azure_auth == "cli":
        from azure.identity import AzureCliCredential

        return AzureCliCredential()
    from azure.identity import DefaultAzureCredential

    return DefaultAzureCredential()


def make_database(settings: Settings) -> Database:
    """Azure SQL con la identidad administrada si hay servidor y base de datos; si no, SQLite en data_dir."""
    if settings.sql_server and settings.sql_database:
        connect = mssql_connector(settings.sql_server, settings.sql_database, _azure_credential(settings))
        return Database(connect, "mssql")
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    return Database.sqlite(settings.data_dir / "meaculpa.db")


CurrentUser = Annotated[User, Depends(current_user)]


def create_app(
    settings: Settings | None = None,
    provider: LLMProvider | None = None,
    token_verifier: TokenVerifier | None = None,
    objects: ObjectStore | None = None,
    models: dict[str, LLMProvider] | None = None,
    geo_settings: GeoSettings | None = None,
    geo_photos: PhotoSet | None = None,
    database: Database | None = None,
    azure_costs: AzureCosts | None = None,
) -> FastAPI:
    """Crea la API. Sin GOOGLE_CLIENT_ID no arranca: todas las rutas salvo salud y modelos exigen login.

    `models`: rivales IA elegibles por id (por defecto, los de LLM_MODELS; sin ninguno, solo `provider`).
    `geo_settings`, `geo_photos`: configuración y fotos de ¿Dónde está esta foto? (por defecto, entorno y data/fotos).
    `database`: usuarios, uso, mensajes y controles (por defecto, Azure SQL si está configurado o SQLite).
    `azure_costs`: gasto de Azure para /admin (por defecto, Cost Management con la identidad administrada).
    """
    settings = settings or get_settings()
    if not settings.google_client_id:
        raise RuntimeError("Falta GOOGLE_CLIENT_ID: la API exige login con Google y no arranca sin él")
    client_id = settings.google_client_id
    verifier = token_verifier or google_verifier(client_id)
    provider = provider or get_provider(settings)
    if models is None:
        models = build_model_providers(settings) if settings.llm_provider != "mock" else {}
    if not models:
        models = {provider.name: provider}
    default_model = settings.llm_default_model if settings.llm_default_model in models else next(iter(models))
    objects = objects or make_object_store(settings)
    data = DataStore(database or make_database(settings))
    geo_settings = geo_settings or get_geo_settings()
    if azure_costs is None:
        fetch = (
            http_fetcher(settings.azure_subscription_id, _azure_credential(settings))
            if settings.azure_subscription_id
            else None
        )
        azure_costs = AzureCosts(fetch, settings.azure_budget_eur)
    store = GameStore(objects)
    limiter = UsageLimiter(objects, settings, overrides=data.quota)
    metrics = MetricsStore(settings.data_dir / "llm_moves.jsonl")
    analysis_slots = threading.BoundedSemaphore(max(1, settings.max_concurrent_analyses))
    _setup_telemetry()

    # Sin /docs ni /openapi.json: no se publica el mapa de la API.
    app = FastAPI(title="Mea culpa", version="0.2.0", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),
        allow_methods=["GET", "POST", "DELETE"],
        allow_headers=["Authorization", "Content-Type"],
        max_age=600,
    )

    @app.middleware("http")
    async def demo_switch(request: Request, call_next):
        """Demo desactivada desde /admin: 503 en todo salvo salud y catálogo de modelos."""
        path = request.url.path
        for demo, prefix in (("meaculpa", API_PREFIX), ("geo", GEO_PREFIX)):
            if (
                path.startswith(prefix + "/")
                and not path.endswith(("/health", "/models"))
                and not await run_in_threadpool(data.demo_enabled, demo)
            ):
                return JSONResponse(
                    {"detail": "Esta demo está desactivada ahora mismo. Vuelve a probar más tarde."}, status_code=503
                )
        return await call_next(request)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        return response

    app.state.store = store
    app.state.provider = provider
    app.state.limiter = limiter
    app.state.authenticate = make_user_dependency(verifier, client_id)
    app.state.data = data

    def _usable(model_id: str) -> bool:
        return model_id in models and data.model_enabled(model_id)

    def _record(user: User, tipo: str, **kw: Any) -> None:
        try:
            data.record(user.owner, "meaculpa", tipo, **kw)
        except Exception:  # noqa: BLE001 - el registro de uso nunca rompe la partida
            log.exception("No se pudo registrar el evento %s", tipo)

    def _get(user: User, game_id: str) -> GameRecord:
        game = store.get(user.owner, game_id)  # solo busca entre las partidas del usuario
        if game is None:
            raise HTTPException(404, "Partida no encontrada")
        return game

    def _save(game: GameRecord) -> None:
        try:
            store.save(game)
        except GameConflictError as exc:
            raise HTTPException(409, "La partida ha cambiado en otra pestaña; recárgala") from exc

    def _opponent_move(user: User, game: GameRecord, board: chess.Board) -> None:
        model_id = game.opponent.get("model") or default_model
        if game.opponent.get("type") == "llm":
            if not data.model_enabled(model_id):
                raise HTTPException(503, "Ese modelo está desactivado ahora mismo; empieza otra partida con otro rival")
            limiter.consume(user.owner, "jugadas_llm")
            limiter.consume_llm_calls(1 + settings.llm_max_retries)  # peor caso: todos los reintentos
        llm = models.get(model_id, provider)
        opp = build_opponent(game.opponent, llm, metrics, settings.llm_max_retries)
        t0 = time.perf_counter()
        decision = opp.choose_move(board)
        meta = dict(decision.meta)
        meta["ms"] = round((time.perf_counter() - t0) * 1000)
        if decision.move not in board.legal_moves:  # defensa en profundidad: nunca debería pasar
            raise HTTPException(500, "El rival devolvió una jugada ilegal")
        meta["san"] = board.san(decision.move)
        board.push(decision.move)
        game.moves.append(decision.move.uci())
        game.last_opponent_meta = meta
        finish_if_over(game, board)
        if game.opponent.get("type") == "llm":
            reg = meta.get("registro") or {}
            _record(
                user,
                "jugada_ia",
                ref=game.id,
                modelo=model_id,
                tokens_in=reg.get("input_tokens", 0),
                tokens_out=reg.get("output_tokens", 0),
                usd=reg.get("cost_usd", 0.0),
            )

    @contextmanager
    def _analysis_slot(user: User, plies: int) -> Iterator[float]:
        """Tope de plies, análisis simultáneos por réplica, cuota del usuario y tiempo máximo.

        Devuelve el instante (time.monotonic) en que se corta el análisis del motor.
        """
        if plies > settings.max_analysis_plies:
            raise HTTPException(
                422, f"Partida demasiado larga para analizarla aquí (máximo {settings.max_analysis_plies} plies)"
            )
        if not analysis_slots.acquire(timeout=5):
            raise HTTPException(503, "Hay otro análisis en marcha; vuelve a intentarlo en unos segundos")
        try:
            limiter.consume(user.owner, "analisis")
            limiter.consume_llm_calls(1)
            yield time.monotonic() + settings.analysis_timeout_s
        except AnalysisTimeout as exc:
            raise HTTPException(503, "El análisis ha tardado demasiado; prueba con una partida más corta") from exc
        finally:
            analysis_slots.release()

    @app.get(API_PREFIX + "/health")
    def health() -> dict[str, Any]:
        return {"ok": True}

    @app.get(API_PREFIX + "/opponents")
    def opponents(user: CurrentUser) -> dict[str, Any]:
        stockfish = find_stockfish() is not None
        return {
            "clasicos": classic_opponent_catalog(),
            "llm": {
                "proveedor": provider.kind,
                "modelo": provider.name,
                "max_reintentos": settings.llm_max_retries,
            },
            "modelos": _catalog(),
            "stockfish": stockfish,
            "motor_analisis": "Stockfish" if stockfish else f"alfa-beta propio (profundidad {settings.analysis_depth})",
            "limites_diarios": limiter.effective_limits(),
            "usuario": user.name,
        }

    @app.get(API_PREFIX + "/models")
    def list_models() -> dict[str, Any]:
        """Público (sin login): el selector de modelo se pinta antes de entrar con Google."""
        return {"modelos": _catalog(), "por_defecto": default_model, "demo_activa": data.demo_enabled("meaculpa")}

    def _catalog() -> list[dict[str, Any]]:
        rows = model_catalog(models, default_model)
        for row in rows:
            row["disponible"] = row["disponible"] and data.model_enabled(row["id"])
        return rows

    @app.post(API_PREFIX + "/games")
    def new_game(req: NewGameRequest, user: CurrentUser) -> dict[str, Any]:
        cfg = req.opponent.model_dump()
        cfg["nombre"] = OPPONENT_NAMES[req.opponent.type]
        if req.opponent.type == "llm":
            model_id = req.opponent.model or default_model
            if not _usable(model_id):
                raise HTTPException(400, "Ese modelo no está disponible ahora mismo")
            cfg["model"] = model_id
            spec = CATALOG_BY_ID.get(model_id)
            cfg["modelo_etiqueta"] = f"{spec.etiqueta}, {spec.descripcion.lower()}" if spec else models[model_id].name
        else:
            cfg.pop("model", None)
        if req.opponent.type == "stockfish" and not find_stockfish():
            raise HTTPException(400, "Stockfish no está disponible en este equipo")
        limiter.consume(user.owner, "partidas")
        game = GameRecord(
            id=new_game_id(), created=time.time(), user_color=req.user_color, opponent=cfg, owner=user.owner
        )
        board = game.board()
        _record(user, "partida", ref=game.id, modelo=cfg.get("model"))
        if game.user_chess_color == chess.BLACK:
            _opponent_move(user, game, board)
        _save(game)
        return game_state(game)

    @app.get(API_PREFIX + "/games")
    def list_games(user: CurrentUser) -> list[dict[str, Any]]:
        return [
            {
                "id": g.id,
                "created": g.created,
                "rival": g.opponent.get("nombre"),
                "user_color": g.user_color,
                "status": g.status,
                "resultado_usuario": g.resultado_usuario,
                "jugadas": len(g.moves),
                "patrones": g.patrones,
                "titular": (g.diagnosis or {}).get("diagnostico", {}).get("titular") if g.diagnosis else None,
            }
            for g in store.recent(user.owner, limit=20)
        ]

    @app.get(API_PREFIX + "/games/{game_id}")
    def get_game(game_id: str, user: CurrentUser) -> dict[str, Any]:
        return game_state(_get(user, game_id))

    @app.post(API_PREFIX + "/games/{game_id}/move")
    def play_move(game_id: str, req: MoveRequest, user: CurrentUser) -> dict[str, Any]:
        game = _get(user, game_id)
        if game.status != "en_curso":
            raise HTTPException(409, "La partida ya ha terminado")
        board = game.board()
        if board.turn != game.user_chess_color:
            raise HTTPException(409, "No es tu turno")
        try:
            move = chess.Move.from_uci(req.uci)
        except ValueError as exc:
            raise HTTPException(422, "Formato UCI inválido") from exc
        if move not in board.legal_moves:
            raise HTTPException(422, "Jugada ilegal")
        limiter.consume(user.owner, "jugadas")
        _record(user, "jugada", ref=game.id)
        board.push(move)
        game.moves.append(move.uci())
        game.last_opponent_meta = None
        finish_if_over(game, board)
        if game.status == "en_curso":
            _opponent_move(user, game, board)
        _save(game)
        return game_state(game)

    @app.post(API_PREFIX + "/games/{game_id}/resign")
    def resign(game_id: str, user: CurrentUser) -> dict[str, Any]:
        game = _get(user, game_id)
        if game.status == "en_curso":
            game.status = "terminada"
            game.resultado_usuario = "derrota"
            game.motivo_fin = "abandono"
            game.result = "0-1" if game.user_color == "white" else "1-0"
            _save(game)
        return game_state(game)

    def _analysis_payload(game: GameRecord) -> dict[str, Any]:
        return {"game_id": game.id, "analisis": game.analysis, **(game.diagnosis or {})}

    @app.post(API_PREFIX + "/games/{game_id}/analysis")
    def analyse_game(game_id: str, user: CurrentUser) -> dict[str, Any]:
        game = _get(user, game_id)
        if not any((i % 2 == 0) == (game.user_color == "white") for i in range(len(game.moves))):
            raise HTTPException(409, "Todavía no has hecho ninguna jugada")
        with _analysis_slot(user, len(game.moves)) as deadline:
            analyzer = make_analyzer(settings, plies=len(game.moves))
            try:
                t0 = time.perf_counter()
                moves = [chess.Move.from_uci(u) for u in game.moves]
                analysis = analyze_moves(moves, game.user_chess_color, analyzer, deadline=deadline)
                engine_ms = (time.perf_counter() - t0) * 1000
            finally:
                analyzer.close()
            history = history_counts(store.previous_patterns(user.owner, game.id), analysis.patrones_partida)
            result_text = {"victoria": "victoria", "derrota": "derrota", "tablas": "tablas"}.get(
                game.resultado_usuario or "", "en curso"
            )
            diag = diagnose(analysis, provider, game.pgn(), result_text, history, settings=settings)
        _record(
            user,
            "analisis",
            ref=game.id,
            modelo=diag.modelo,
            tokens_in=diag.input_tokens,
            tokens_out=diag.output_tokens,
            usd=diag.coste_usd,
        )
        game.analysis = {**analysis.to_dict(), "motor_ms": round(engine_ms), "historial": history}
        game.patrones = analysis.patrones_partida
        game.diagnosis = asdict(diag)
        _save(game)
        return _analysis_payload(game)

    @app.get(API_PREFIX + "/games/{game_id}/analysis")
    def get_analysis(game_id: str, user: CurrentUser) -> dict[str, Any]:
        game = _get(user, game_id)
        if game.analysis is None:
            raise HTTPException(404, "Esta partida aún no tiene análisis")
        return _analysis_payload(game)

    @app.post(API_PREFIX + "/analysis/pgn")
    def analyse_pgn(req: PgnAnalysisRequest, user: CurrentUser) -> dict[str, Any]:
        """Analiza un PGN externo (dato no fiable: se valida y se reconstruye sin comentarios ni cabeceras)."""
        try:
            pgn_game = parse_pgn(req.pgn)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        moves = list(pgn_game.mainline_moves())
        color = chess.WHITE if req.user_color == "white" else chess.BLACK
        with _analysis_slot(user, len(moves)) as deadline:
            analyzer = make_analyzer(settings, plies=len(moves))
            try:
                analysis = analyze_moves(moves, color, analyzer, pgn_game.board().fen(), deadline=deadline)
            finally:
                analyzer.close()
            history = history_counts([], analysis.patrones_partida)
            pgn = pgn_without_comments(pgn_game)
            diag = diagnose(analysis, provider, pgn, "desconocido", history, settings=settings)
        _record(
            user,
            "analisis_pgn",
            modelo=diag.modelo,
            tokens_in=diag.input_tokens,
            tokens_out=diag.output_tokens,
            usd=diag.coste_usd,
        )
        return {"analisis": analysis.to_dict(), **asdict(diag)}

    @app.get(API_PREFIX + "/metrics/llm")
    def llm_metrics(user: CurrentUser) -> dict[str, Any]:
        return {"resumen": metrics.summary()}

    # ¿Dónde está esta foto? (danielalonso.dev/donde-esta): mismo login, almacén y cuotas, bajo /api/geo.
    @app.get(GEO_PREFIX + "/health")
    def geo_health() -> dict[str, Any]:
        return {"ok": True}

    app.include_router(
        create_geo_router(
            current_user=current_user,
            objects=objects,
            settings=settings,
            models=models,
            geo_settings=geo_settings,
            photos=geo_photos,
            data=data,
        )
    )
    app.include_router(create_contact_router(data=data, settings=settings))
    app.include_router(
        create_admin_router(
            authenticate=app.state.authenticate,
            admin_emails=settings.admin_emails,
            data=data,
            costs=azure_costs,
            models=models,
            quota_defaults={
                **limiter.limits,
                "llm_global": settings.global_llm_calls_per_day,
                "geo_partidas": geo_settings.quota_games_per_day,
                "geo_ia": geo_settings.quota_llm_per_day,
                "geo_global": geo_settings.global_llm_per_day,
            },
        )
    )

    return app
