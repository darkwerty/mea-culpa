"""API FastAPI de Mea culpa."""

from __future__ import annotations

import logging
import os
import time
from dataclasses import asdict
from typing import Any, Literal

import chess
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from .analysis import analyze_moves, diagnose, history_counts, make_analyzer, parse_pgn, pgn_without_comments
from .config import Settings, get_settings
from .engines import classic_opponent_catalog, find_stockfish
from .games import GameRecord, GameStore, build_opponent, finish_if_over, game_state, new_game_id
from .llm import LLMProvider, get_provider
from .llm_player import MetricsStore

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
    pgn: str = Field(..., max_length=50_000)
    user_color: Literal["white", "black"]


OPPONENT_NAMES = {
    "random": "Aleatorio",
    "greedy": "Codicioso",
    "minimax": "Minimax",
    "stockfish": "Stockfish",
    "llm": "IA de Foundry",
}


def create_app(settings: Settings | None = None, provider: LLMProvider | None = None) -> FastAPI:
    settings = settings or get_settings()
    provider = provider or get_provider(settings)
    store = GameStore(settings.data_dir)
    metrics = MetricsStore(settings.data_dir / "llm_moves.jsonl")
    _setup_telemetry()

    app = FastAPI(title="Mea culpa", version="0.1.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.state.store = store
    app.state.provider = provider

    def _get(game_id: str) -> GameRecord:
        game = store.get(game_id)
        if game is None:
            raise HTTPException(404, "Partida no encontrada")
        return game

    def _opponent_move(game: GameRecord, board: chess.Board) -> None:
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

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {
            "ok": True,
            "llm": {"proveedor": provider.kind, "modelo": provider.name},
            "stockfish": find_stockfish() is not None,
            "motor_analisis": "Stockfish"
            if find_stockfish()
            else f"alfa-beta propio (profundidad {settings.analysis_depth})",
        }

    @app.get("/api/opponents")
    def opponents() -> dict[str, Any]:
        return {
            "clasicos": classic_opponent_catalog(),
            "llm": {
                "proveedor": provider.kind,
                "modelo": provider.name,
                "max_reintentos": settings.llm_max_retries,
            },
        }

    @app.post("/api/games")
    def new_game(req: NewGameRequest) -> dict[str, Any]:
        cfg = req.opponent.model_dump()
        cfg["nombre"] = OPPONENT_NAMES[req.opponent.type]
        if req.opponent.type == "stockfish" and not find_stockfish():
            raise HTTPException(400, "Stockfish no está disponible en este equipo")
        game = GameRecord(id=new_game_id(), created=time.time(), user_color=req.user_color, opponent=cfg)
        board = game.board()
        if game.user_chess_color == chess.BLACK:
            _opponent_move(game, board)
        store.save(game)
        return game_state(game)

    @app.get("/api/games")
    def list_games() -> list[dict[str, Any]]:
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
            for g in store.all()
        ]

    @app.get("/api/games/{game_id}")
    def get_game(game_id: str) -> dict[str, Any]:
        return game_state(_get(game_id))

    @app.post("/api/games/{game_id}/move")
    def play_move(game_id: str, req: MoveRequest) -> dict[str, Any]:
        game = _get(game_id)
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
        board.push(move)
        game.moves.append(move.uci())
        game.last_opponent_meta = None
        finish_if_over(game, board)
        if game.status == "en_curso":
            _opponent_move(game, board)
        store.save(game)
        return game_state(game)

    @app.post("/api/games/{game_id}/resign")
    def resign(game_id: str) -> dict[str, Any]:
        game = _get(game_id)
        if game.status == "en_curso":
            game.status = "terminada"
            game.resultado_usuario = "derrota"
            game.motivo_fin = "abandono"
            game.result = "0-1" if game.user_color == "white" else "1-0"
            store.save(game)
        return game_state(game)

    def _analysis_payload(game: GameRecord) -> dict[str, Any]:
        return {"game_id": game.id, "analisis": game.analysis, **(game.diagnosis or {})}

    @app.post("/api/games/{game_id}/analysis")
    def analyse_game(game_id: str) -> dict[str, Any]:
        game = _get(game_id)
        if not any((i % 2 == 0) == (game.user_color == "white") for i in range(len(game.moves))):
            raise HTTPException(409, "Todavía no has hecho ninguna jugada")
        analyzer = make_analyzer(settings, plies=len(game.moves))
        try:
            t0 = time.perf_counter()
            analysis = analyze_moves([chess.Move.from_uci(u) for u in game.moves], game.user_chess_color, analyzer)
            engine_ms = (time.perf_counter() - t0) * 1000
        finally:
            analyzer.close()
        history = history_counts(store.previous_patterns(game.id), analysis.patrones_partida)
        result_text = {"victoria": "victoria", "derrota": "derrota", "tablas": "tablas"}.get(
            game.resultado_usuario or "", "en curso"
        )
        diag = diagnose(analysis, provider, game.pgn(), result_text, history, settings=settings)
        game.analysis = {**analysis.to_dict(), "motor_ms": round(engine_ms), "historial": history}
        game.patrones = analysis.patrones_partida
        game.diagnosis = asdict(diag)
        store.save(game)
        return _analysis_payload(game)

    @app.get("/api/games/{game_id}/analysis")
    def get_analysis(game_id: str) -> dict[str, Any]:
        game = _get(game_id)
        if game.analysis is None:
            raise HTTPException(404, "Esta partida aún no tiene análisis")
        return _analysis_payload(game)

    @app.post("/api/analysis/pgn")
    def analyse_pgn(req: PgnAnalysisRequest) -> dict[str, Any]:
        """Analiza un PGN externo (dato no fiable: se valida y se le quitan los comentarios)."""
        try:
            pgn_game = parse_pgn(req.pgn)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        moves = list(pgn_game.mainline_moves())
        color = chess.WHITE if req.user_color == "white" else chess.BLACK
        analyzer = make_analyzer(settings, plies=len(moves))
        try:
            analysis = analyze_moves(moves, color, analyzer, pgn_game.board().fen())
        finally:
            analyzer.close()
        history = history_counts([], analysis.patrones_partida)
        diag = diagnose(analysis, provider, pgn_without_comments(pgn_game), "desconocido", history, settings=settings)
        return {"analisis": analysis.to_dict(), **asdict(diag)}

    @app.get("/api/metrics/llm")
    def llm_metrics() -> dict[str, Any]:
        return {"resumen": metrics.summary()}

    return app
