"""Partidas: estado, rivales y persistencia en ficheros JSON (suficiente para un solo usuario)."""

from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import chess
import chess.pgn

from .engines import GreedyOpponent, MinimaxOpponent, RandomOpponent, StockfishOpponent, find_stockfish
from .llm.base import LLMProvider
from .llm_player import LLMOpponent, MetricsStore

TERMINATIONS = {
    chess.Termination.CHECKMATE: "jaque mate",
    chess.Termination.STALEMATE: "rey ahogado",
    chess.Termination.INSUFFICIENT_MATERIAL: "material insuficiente",
    chess.Termination.SEVENTYFIVE_MOVES: "regla de 75 jugadas",
    chess.Termination.FIVEFOLD_REPETITION: "quíntuple repetición",
    chess.Termination.FIFTY_MOVES: "regla de 50 jugadas",
    chess.Termination.THREEFOLD_REPETITION: "triple repetición",
}


@dataclass
class GameRecord:
    id: str
    created: float
    user_color: str  # "white" | "black"
    opponent: dict[str, Any]
    moves: list[str] = field(default_factory=list)  # UCI
    status: str = "en_curso"  # en_curso | terminada
    result: str = "*"
    resultado_usuario: str | None = None  # victoria | derrota | tablas
    motivo_fin: str | None = None
    last_opponent_meta: dict[str, Any] | None = None
    analysis: dict[str, Any] | None = None
    diagnosis: dict[str, Any] | None = None
    patrones: list[str] = field(default_factory=list)

    @property
    def user_chess_color(self) -> chess.Color:
        return chess.WHITE if self.user_color == "white" else chess.BLACK

    def board(self) -> chess.Board:
        b = chess.Board()
        for uci in self.moves:
            b.push_uci(uci)
        return b

    def pgn(self) -> str:
        game = chess.pgn.Game()
        game.headers["Event"] = "Mea culpa"
        game.headers["Date"] = time.strftime("%Y.%m.%d", time.localtime(self.created))
        rival = self.opponent.get("nombre", self.opponent.get("type", "rival"))
        game.headers["White"] = "Tú" if self.user_color == "white" else rival
        game.headers["Black"] = rival if self.user_color == "white" else "Tú"
        game.headers["Result"] = self.result
        node: chess.pgn.GameNode = game
        for uci in self.moves:
            node = node.add_variation(chess.Move.from_uci(uci))
        return str(game)


class GameStore:
    def __init__(self, data_dir: Path) -> None:
        self.dir = data_dir / "games"
        self.dir.mkdir(parents=True, exist_ok=True)
        self._cache: dict[str, GameRecord] = {}
        self._lock = threading.Lock()

    def save(self, game: GameRecord) -> None:
        with self._lock:
            self._cache[game.id] = game
            path = self.dir / f"{game.id}.json"
            path.write_text(json.dumps(asdict(game), ensure_ascii=False, indent=1), encoding="utf-8")

    def get(self, game_id: str) -> GameRecord | None:
        if game_id in self._cache:
            return self._cache[game_id]
        if not all(c in "0123456789abcdef" for c in game_id):
            return None
        path = self.dir / f"{game_id}.json"
        if not path.exists():
            return None
        game = GameRecord(**json.loads(path.read_text(encoding="utf-8")))
        self._cache[game.id] = game
        return game

    def all(self) -> list[GameRecord]:
        games = []
        for path in self.dir.glob("*.json"):
            g = self.get(path.stem)
            if g:
                games.append(g)
        return sorted(games, key=lambda g: g.created, reverse=True)

    def previous_patterns(self, exclude_id: str) -> list[list[str]]:
        return [g.patrones for g in self.all() if g.id != exclude_id and g.analysis is not None]


def new_game_id() -> str:
    return uuid.uuid4().hex[:12]


def build_opponent(cfg: dict[str, Any], provider: LLMProvider, metrics: MetricsStore, max_retries: int):
    kind = cfg.get("type")
    if kind == "random":
        return RandomOpponent()
    if kind == "greedy":
        return GreedyOpponent()
    if kind == "minimax":
        return MinimaxOpponent(depth=int(cfg.get("depth", 3)))
    if kind == "stockfish":
        path = find_stockfish()
        if not path:
            raise ValueError("Stockfish no está instalado en este equipo")
        return StockfishOpponent(path, skill=int(cfg.get("skill", 5)))
    if kind == "llm":
        return LLMOpponent(
            provider,
            temperature=float(cfg.get("temperature", 0.7)),
            max_retries=max_retries,
            show_legal_moves=bool(cfg.get("show_legal_moves", False)),
            metrics=metrics,
        )
    raise ValueError(f"Rival desconocido: {kind}")


def finish_if_over(game: GameRecord, board: chess.Board) -> None:
    outcome = board.outcome(claim_draw=True)
    if outcome is None:
        return
    game.status = "terminada"
    game.result = outcome.result()
    game.motivo_fin = TERMINATIONS.get(outcome.termination, outcome.termination.name.lower())
    if outcome.winner is None:
        game.resultado_usuario = "tablas"
    else:
        game.resultado_usuario = "victoria" if outcome.winner == game.user_chess_color else "derrota"


def game_state(game: GameRecord) -> dict[str, Any]:
    board = game.board()
    replay = chess.Board()
    san = []
    for uci in game.moves:
        m = chess.Move.from_uci(uci)
        san.append(replay.san(m))
        replay.push(m)
    user_turn = game.status == "en_curso" and board.turn == game.user_chess_color
    return {
        "id": game.id,
        "fen": board.fen(),
        "turn": "white" if board.turn == chess.WHITE else "black",
        "user_color": game.user_color,
        "opponent": game.opponent,
        "moves_uci": game.moves,
        "moves_san": san,
        "legal_moves": [m.uci() for m in board.legal_moves] if user_turn else [],
        "in_check": board.is_check(),
        "status": game.status,
        "result": game.result,
        "resultado_usuario": game.resultado_usuario,
        "motivo_fin": game.motivo_fin,
        "last_move": game.moves[-1] if game.moves else None,
        "last_opponent_meta": game.last_opponent_meta,
        "has_diagnosis": game.diagnosis is not None,
        "pgn": game.pgn(),
    }
