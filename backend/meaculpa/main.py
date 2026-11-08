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
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

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
from .llm import LLMProvider, get_provider
from .llm_player import MetricsStore
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
    temperature: float = Field(0.7, ge=0.0, le=2.0)
    show_legal_moves: bool = False


class NewGameRequest(BaseModel):
    opponent: OpponentConfig
    user_color: Literal["white", "black"] = "white"


class MoveRequest(BaseModel):
    uci: str = Field(..., min_length=4, max_length=5)


class PgnAnalysisRequest(BaseModel):
    pgn: str = Field(..., max_length=20_000)
    user_color: Literal["white", "black"]


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
    """Dependencia de FastAPI: verifica el ID token de Google con el verificador de esta app."""
    return request.app.state.authenticate(request)


CurrentUser = Annotated[User, Depends(current_user)]


def create_app(
    settings: Settings | None = None,
    provider: LLMProvider | None = None,
    token_verifier: TokenVerifier | None = None,
    objects: ObjectStore | None = None,
) -> FastAPI:
    """Crea la API. Sin GOOGLE_CLIENT_ID no arranca: todas las rutas salvo /api/health exigen login."""
    settings = settings or get_settings()
    if not settings.google_client_id:
        raise RuntimeError("Falta GOOGLE_CLIENT_ID: la API exige login con Google y no arranca sin él")
    client_id = settings.google_client_id
    verifier = token_verifier or google_verifier(client_id)
    provider = provider or get_provider(settings)
    objects = objects or make_object_store(settings)
    store = GameStore(objects)
    limiter = UsageLimiter(objects, settings)
    metrics = MetricsStore(settings.data_dir / "llm_moves.jsonl")
    analysis_slots = threading.BoundedSemaphore(max(1, settings.max_concurrent_analyses))
    _setup_telemetry()

    # Sin /docs ni /openapi.json: no se publica el mapa de la API.
    app = FastAPI(title="Mea culpa", version="0.2.0", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),
        allow_methods=["GET", "POST"],
        allow_headers=["Authorization", "Content-Type"],
        max_age=600,
    )

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
        if game.opponent.get("type") == "llm":
            limiter.consume(user.owner, "jugadas_llm")
            limiter.consume_llm_calls(1 + settings.llm_max_retries)  # peor caso: todos los reintentos
        opp = build_opponent(game.opponent, provider, metrics, settings.llm_max_retries)
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

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {"ok": True}

    @app.get("/api/opponents")
    def opponents(user: CurrentUser) -> dict[str, Any]:
        stockfish = find_stockfish() is not None
        return {
            "clasicos": classic_opponent_catalog(),
            "llm": {
                "proveedor": provider.kind,
                "modelo": provider.name,
                "max_reintentos": settings.llm_max_retries,
            },
            "stockfish": stockfish,
            "motor_analisis": "Stockfish" if stockfish else f"alfa-beta propio (profundidad {settings.analysis_depth})",
            "limites_diarios": limiter.limits,
            "usuario": user.name,
        }

    @app.post("/api/games")
    def new_game(req: NewGameRequest, user: CurrentUser) -> dict[str, Any]:
        cfg = req.opponent.model_dump()
        cfg["nombre"] = OPPONENT_NAMES[req.opponent.type]
        if req.opponent.type == "stockfish" and not find_stockfish():
            raise HTTPException(400, "Stockfish no está disponible en este equipo")
        limiter.consume(user.owner, "partidas")
        game = GameRecord(
            id=new_game_id(), created=time.time(), user_color=req.user_color, opponent=cfg, owner=user.owner
        )
        board = game.board()
        if game.user_chess_color == chess.BLACK:
            _opponent_move(user, game, board)
        _save(game)
        return game_state(game)

    @app.get("/api/games")
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

    @app.get("/api/games/{game_id}")
    def get_game(game_id: str, user: CurrentUser) -> dict[str, Any]:
        return game_state(_get(user, game_id))

    @app.post("/api/games/{game_id}/move")
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
        board.push(move)
        game.moves.append(move.uci())
        game.last_opponent_meta = None
        finish_if_over(game, board)
        if game.status == "en_curso":
            _opponent_move(user, game, board)
        _save(game)
        return game_state(game)

    @app.post("/api/games/{game_id}/resign")
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

    @app.post("/api/games/{game_id}/analysis")
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
        game.analysis = {**analysis.to_dict(), "motor_ms": round(engine_ms), "historial": history}
        game.patrones = analysis.patrones_partida
        game.diagnosis = asdict(diag)
        _save(game)
        return _analysis_payload(game)

    @app.get("/api/games/{game_id}/analysis")
    def get_analysis(game_id: str, user: CurrentUser) -> dict[str, Any]:
        game = _get(user, game_id)
        if game.analysis is None:
            raise HTTPException(404, "Esta partida aún no tiene análisis")
        return _analysis_payload(game)

    @app.post("/api/analysis/pgn")
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
        return {"analisis": analysis.to_dict(), **asdict(diag)}

    @app.get("/api/metrics/llm")
    def llm_metrics(user: CurrentUser) -> dict[str, Any]:
        return {"resumen": metrics.summary()}

    return app
