"""API FastAPI de Mea culpa."""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Iterator
from contextlib import asynccontextmanager, contextmanager, suppress
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
    pgn_player_result,
    pgn_without_comments,
    player_result,
)
from .auth import TokenVerifier, User, google_verifier, make_user_dependency
from .config import Settings, get_settings
from .contacto import create_contact_router
from .costes_azure import AzureCosts, http_fetcher
from .datos import DataStore
from .db import Database, mssql_connector
from .engines import classic_opponent_catalog, find_stockfish
from .estadisticas import WebStats, create_stats_router
from .games import (
    GameConflictError,
    GameRecord,
    GameStore,
    build_opponent,
    finish_if_over,
    game_state,
    mark_help,
    new_game_id,
    take_back,
)
from .geo import GEO_PREFIX, create_geo_router, geo_quota_defaults
from .geo.config import GeoSettings, get_geo_settings
from .geo.photos import PhotoSet
from .hints import HintText, explain, fallback_explanation, motives
from .live import (
    EngineBusy,
    EngineFailure,
    EvalRateLimiter,
    LiveEngine,
    eval_payload,
    pv_san,
    terminal_payload,
)
from .llm import LLMProvider, get_provider
from .llm_player import MetricsStore
from .models import CATALOG_BY_ID, MODEL_CATALOG, build_model_providers, model_catalog, model_label
from .partidas import chess_row
from .quota import UsageLimiter
from .ranking import RankingStore, chess_rival, create_ranking_router
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
    ranking: bool = True  # casilla «Salir en el ranking con mi nombre» (marcada por defecto)


class MoveRequest(BaseModel):
    uci: str = Field(..., min_length=4, max_length=5)


class EvalRequest(BaseModel):
    fen: str = Field(..., min_length=10, max_length=120)


class TakebackRequest(BaseModel):
    ply: int = Field(..., ge=0, le=2000)


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


_HELP_FIELDS = ("con_ayuda", "ayudas", "etag")


def _snapshot(game: GameRecord) -> dict[str, Any]:
    """Copia profunda de la partida tal como se ha leído, para comparar al guardar."""
    return asdict(game)


def _without_help(data: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in data.items() if k not in _HELP_FIELDS}


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
    live_engine: LiveEngine | None = None,
) -> FastAPI:
    """Crea la API. Sin GOOGLE_CLIENT_ID no arranca: todas las rutas salvo salud y modelos exigen login.

    `models`: rivales IA elegibles por id (por defecto, los de LLM_MODELS; sin ninguno, solo `provider`).
    `geo_settings`, `geo_photos`: configuración y fotos de ¿Dónde está esta foto? (por defecto, entorno y data/fotos).
    `database`: usuarios, uso, mensajes y controles (por defecto, Azure SQL si está configurado o SQLite).
    `azure_costs`: gasto de Azure para /admin (por defecto, Cost Management con la identidad administrada).
    `live_engine`: motor de la barra de evaluación y las pistas (por defecto, un pool de Stockfish o el alfa-beta).
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
    ranking = RankingStore(data.db, objects)
    # Modelos que juegan al ajedrez (los del catálogo marcados `ajedrez` y los de fuera del catálogo, como el mock).
    chess_ids = {m for m in models if m not in CATALOG_BY_ID or CATALOG_BY_ID[m].ajedrez}
    metrics = MetricsStore(settings.data_dir / "llm_moves.jsonl")
    analysis_slots = threading.BoundedSemaphore(max(1, settings.max_concurrent_analyses))
    live = live_engine or LiveEngine(size=settings.engine_pool_size, timeout_s=settings.engine_timeout_s)
    eval_limiter = EvalRateLimiter(settings.eval_interval_ms / 1000, settings.eval_per_day)
    _setup_telemetry()

    # Sin /docs ni /openapi.json: no se publica el mapa de la API.
    web_stats = WebStats(data.db, objects)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        web_stats.persist()  # al apagarse (escala a cero), las visitas en memoria pasan a Blob
        live.close()  # sin procesos de Stockfish huérfanos

    app = FastAPI(
        title="Mea culpa", version="0.2.0", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan
    )
    app.state.web_stats = web_stats
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
    app.state.live = live
    app.state.eval_limiter = eval_limiter

    def _usable(model_id: str) -> bool:
        return model_id in chess_ids and data.model_enabled(model_id)

    def _rank(user: User, game: GameRecord) -> None:
        """Partida terminada (jaque mate, tablas o abandono) al ranking, con el resultado que da el servidor.

        Las partidas con ayuda (pistas o jugadas deshechas) no cuentan: ni en el ranking ni en «Humanos contra IA».
        """
        if game.status != "terminada" or game.resultado_usuario not in ("victoria", "tablas", "derrota"):
            return
        if game.con_ayuda:
            return
        rival, rival_name = chess_rival(game.opponent)
        try:
            ranking.record("meaculpa", game.id, user, rival=rival, rival_name=rival_name, result=game.resultado_usuario)
        except Exception:  # noqa: BLE001 - el ranking nunca rompe la partida
            log.exception("No se pudo guardar la partida %s en el ranking", game.id)

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

    def _save(game: GameRecord, base: dict[str, Any] | None = None) -> None:
        """Guarda con control de versión. `base`: la partida tal como se leyó (`_snapshot`).

        Si entretanto otra petición solo ha marcado ayuda (una pista pedida a la vez que una jugada), no se pierde
        ninguna de las dos: se suman las ayudas sobre la versión nueva y se vuelve a guardar. Cualquier otro cambio
        (otra jugada, deshacer...) es un conflicto de verdad: 409.
        """
        try:
            store.save(game)
        except GameConflictError as exc:
            fresh = store.get(game.owner, game.id) if base is not None else None
            if fresh is None or _without_help(_snapshot(fresh)) != _without_help(base or {}):
                raise HTTPException(409, "La partida ha cambiado en otra pestaña; recárgala") from exc
            before = base.get("ayudas") or {} if base else {}
            mine = game.ayudas or {}
            theirs = fresh.ayudas or {}
            game.ayudas = {
                k: int(theirs.get(k, 0)) + int(mine.get(k, 0)) - int(before.get(k, 0)) for k in ("pistas", "deshacer")
            }
            game.con_ayuda = game.con_ayuda or fresh.con_ayuda
            game.etag = fresh.etag
            try:
                store.save(game)
            except GameConflictError as exc2:
                raise HTTPException(409, "La partida ha cambiado en otra pestaña; recárgala") from exc2
        try:
            data.save_game(chess_row(game, time.time()))  # copia en SQL para /admin (cola en segundo plano)
        except Exception:  # noqa: BLE001 - nunca rompe la partida
            log.exception("No se pudo registrar la partida %s", game.id)

    def _opponent_move(user: User, game: GameRecord, board: chess.Board) -> None:
        model_id = game.opponent.get("model") or default_model
        if game.opponent.get("type") == "llm":
            if not data.model_enabled(model_id):
                raise HTTPException(503, "Ese modelo está desactivado ahora mismo; empieza otra partida con otro rival")
            limiter.check_budget(user.owner)
            limiter.consume(user.owner, "jugadas_llm")
            limiter.consume_llm_calls(1 + settings.llm_max_retries)  # peor caso: todos los reintentos
        llm = models.get(model_id, provider)
        opp = build_opponent(game.opponent, llm, metrics, settings.llm_max_retries)
        t0 = time.perf_counter()
        try:
            decision = opp.choose_move(board)
        except Exception as exc:  # noqa: BLE001 - error del proveedor (red, 4xx/5xx): la jugada no se guarda
            if game.opponent.get("type") != "llm":
                raise
            log.warning("El modelo %s no ha respondido: %s", model_id, exc)
            raise HTTPException(503, "El modelo no ha respondido. Vuelve a hacer tu jugada en unos segundos.") from exc
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
            limiter.add_cost(float(reg.get("cost_usd", 0.0) or 0.0), user.owner)
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
            limiter.check_budget(user.owner)
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
            "motor_vivo": live.name,
            "limites_diarios": limiter.effective_limits(),
            "usuario": user.name,
        }

    @app.get(API_PREFIX + "/models")
    def list_models() -> dict[str, Any]:
        """Público (sin login): el selector de modelo se pinta antes de entrar con Google."""
        return {"modelos": _catalog(), "por_defecto": default_model, "demo_activa": data.demo_enabled("meaculpa")}

    def _catalog() -> list[dict[str, Any]]:
        rows = model_catalog(models, default_model, only={m.id for m in MODEL_CATALOG if m.ajedrez})
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
            cfg["modelo_etiqueta"] = model_label(model_id, models[model_id].name)
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
        ranking.set_preference(user, req.ranking)
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
                "con_ayuda": g.con_ayuda,
            }
            for g in store.recent(user.owner, limit=20)
        ]

    @app.get(API_PREFIX + "/games/{game_id}")
    def get_game(game_id: str, user: CurrentUser) -> dict[str, Any]:
        return game_state(_get(user, game_id))

    @app.post(API_PREFIX + "/games/{game_id}/move")
    def play_move(game_id: str, req: MoveRequest, user: CurrentUser) -> dict[str, Any]:
        game = _get(user, game_id)
        base = _snapshot(game)
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
        _save(game, base)
        _rank(user, game)
        return game_state(game)

    @app.post(API_PREFIX + "/games/{game_id}/resign")
    def resign(game_id: str, user: CurrentUser) -> dict[str, Any]:
        game = _get(user, game_id)
        base = _snapshot(game)
        if game.status == "en_curso":
            game.status = "terminada"
            game.resultado_usuario = "derrota"
            game.motivo_fin = "abandono"
            game.result = "0-1" if game.user_color == "white" else "1-0"
            _save(game, base)
            _rank(user, game)
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
            result_text = player_result(game.resultado_usuario)
            diag = diagnose(
                analysis, provider, game.pgn(), result_text, history, settings=settings, end_reason=game.motivo_fin
            )
            limiter.add_cost(diag.coste_usd, user.owner)
            if diag.llamadas > 1:  # reintento por incoherencia con el resultado: cuenta en el fusible, sin cortar
                with suppress(HTTPException):
                    limiter.consume_llm_calls(diag.llamadas - 1)
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
            result_text = pgn_player_result(pgn_game.headers.get("Result", "*"), color)
            diag = diagnose(analysis, provider, pgn, result_text, history, settings=settings)
            limiter.add_cost(diag.coste_usd, user.owner)
            if diag.llamadas > 1:
                with suppress(HTTPException):
                    limiter.consume_llm_calls(diag.llamadas - 1)
        _record(
            user,
            "analisis_pgn",
            modelo=diag.modelo,
            tokens_in=diag.input_tokens,
            tokens_out=diag.output_tokens,
            usd=diag.coste_usd,
        )
        return {"analisis": analysis.to_dict(), **asdict(diag)}

    # ------------------------------------------------ barra de evaluación, pistas y deshacer
    @contextmanager
    def _engine_errors() -> Iterator[None]:
        try:
            yield
        except EngineBusy as exc:
            raise HTTPException(503, "El motor está ocupado; vuelve a intentarlo en un momento") from exc
        except EngineFailure as exc:
            raise HTTPException(503, "El motor no ha respondido; vuelve a intentarlo") from exc

    @app.post(API_PREFIX + "/eval")
    def evaluate_position(req: EvalRequest, user: CurrentUser) -> dict[str, Any]:
        """Barra de evaluación: solo motor (sin LLM ni base de datos), tiempo corto. Puntuación desde las blancas."""
        try:
            board = chess.Board(req.fen.strip())
        except ValueError as exc:
            raise HTTPException(422, "FEN no válido") from exc
        if not board.is_valid():
            raise HTTPException(422, "Esa posición no es legal")
        why = eval_limiter.allow(user.owner)
        if why == "rafaga":
            raise HTTPException(429, "Demasiadas evaluaciones seguidas; espera un momento")
        if why == "diario":
            raise HTTPException(429, "Has llegado al límite diario de evaluaciones. Vuelve mañana.")
        terminal = terminal_payload(board)
        if terminal is not None:
            return terminal
        with _engine_errors():
            lines = live.analyse(board, time_s=settings.eval_time_ms / 1000, depth=settings.eval_depth)
        return eval_payload(board, lines)

    @app.post(API_PREFIX + "/games/{game_id}/hint")
    def hint(game_id: str, user: CurrentUser) -> dict[str, Any]:
        """La mejor jugada para la posición actual, los motivos del motor y por qué (LLM o, si falla, sin LLM)."""
        game = _get(user, game_id)
        base = _snapshot(game)
        if game.status != "en_curso":
            raise HTTPException(409, "La partida ya ha terminado")
        board = game.board()
        if board.turn != game.user_chess_color:
            raise HTTPException(409, "No es tu turno")
        limiter.consume(user.owner, "pistas")
        with _engine_errors():
            lines = live.analyse(board, time_s=settings.hint_time_ms / 1000, depth=settings.hint_depth, multipv=2)
            best = lines[0].pv[0] if lines and lines[0].pv else None
            if best is None or best not in board.legal_moves:
                raise EngineFailure("sin jugada")
            after = board.copy(stack=False)
            after.push(best)
            after_terminal = terminal_payload(after)
            after_line = (
                None
                if after_terminal is not None
                else live.analyse(after, time_s=settings.eval_time_ms / 1000, depth=settings.eval_depth)[0]
            )
        best_san = board.san(best)
        reasons = motives(board, best, lines, after_line)
        line = pv_san(board, lines[0].pv)
        try:
            limiter.check_budget(user.owner)
            limiter.consume_llm_calls(1)
        except HTTPException as exc:  # sin presupuesto o sin llamadas: explicación sin LLM
            text = HintText(fallback_explanation(best_san, reasons), "motor", error=str(exc.detail))
        else:
            text = explain(provider, board, best_san, line, reasons, settings)
            limiter.add_cost(text.coste_usd, user.owner)
        _record(
            user,
            "pista",
            ref=game.id,
            modelo=text.modelo,
            tokens_in=text.input_tokens,
            tokens_out=text.output_tokens,
            usd=text.coste_usd,
        )
        mark_help(game, "pistas")
        try:
            _save(game, base)
        except HTTPException as exc:
            # Has movido (o deshecho) mientras se calculaba: la pista ya no vale para la posición y no se cobra.
            limiter.refund(user.owner, "pistas")
            raise HTTPException(409, "La partida ha cambiado mientras se calculaba la pista; vuelve a pedirla") from exc
        if after_terminal is not None:
            eval_after = {"cp": after_terminal["cp"], "mate": after_terminal["mate"]}
        else:
            assert after_line is not None
            eval_after = after_line.score()
        return {
            "best_uci": best.uci(),
            "best_san": best_san,
            "pv_san": line,
            "eval_antes": lines[0].score(),
            "eval_despues": eval_after,
            "motivos": reasons,
            "explicacion": text.explicacion,
            "fuente": text.fuente,
            "con_ayuda": game.con_ayuda,
            "ayudas": dict(game.ayudas),
        }

    @app.post(API_PREFIX + "/games/{game_id}/takeback")
    def takeback(game_id: str, req: TakebackRequest, user: CurrentUser) -> dict[str, Any]:
        """Vuelve a una posición anterior en la que mueve el jugador (reabre la partida si había terminado)."""
        game = _get(user, game_id)
        if req.ply == len(game.moves) and game.status == "en_curso":
            return game_state(game)  # nada que deshacer
        try:
            take_back(game, req.ply)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        mark_help(game, "deshacer")
        _save(game)
        # Si ya había terminado sin ayuda, su resultado se queda en el ranking: reabrirla no lo borra (si no,
        # bastaría con deshacer después de perder para quitarse las derrotas). Lo que pase después ya no cuenta.
        _record(user, "deshacer", ref=game.id)
        return game_state(game)

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
            ranking=ranking,
            budget=limiter,
        )
    )
    app.include_router(create_ranking_router(current_user=current_user, ranking=ranking))
    app.state.ranking = ranking
    app.include_router(create_contact_router(data=data, settings=settings))
    app.include_router(create_stats_router(web_stats))
    app.include_router(
        create_admin_router(
            authenticate=app.state.authenticate,
            admin_emails=settings.admin_emails,
            data=data,
            costs=azure_costs,
            web_stats=web_stats,
            models=models,
            quota_defaults={
                **limiter.limits,
                "llm_global": settings.global_llm_calls_per_day,
                "ia_centimos_dia": settings.global_llm_cents_per_day,
                "ia_centimos_usuario": settings.user_llm_cents_per_day,
                **geo_quota_defaults(geo_settings),
            },
        )
    )

    return app
