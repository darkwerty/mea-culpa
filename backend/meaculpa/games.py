"""Partidas: estado, rivales y persistencia por usuario (Blob Storage en Azure, ficheros en local)."""

from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

import chess
import chess.pgn

from .engines import GreedyOpponent, MinimaxOpponent, RandomOpponent, StockfishOpponent, find_stockfish
from .llm.base import LLMProvider
from .llm_player import LLMOpponent, MetricsStore
from .storage import ObjectStore

_HEX = re.compile(r"^[0-9a-f]{12,64}$")

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
    owner: str = ""  # sha256 del `sub` de Google: nunca el email
    etag: str | None = field(default=None, repr=False, compare=False)  # no se guarda

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


class GameConflictError(Exception):
    """La partida cambió entre la lectura y la escritura (dos peticiones a la vez)."""


class GameStore:
    """Partidas por usuario: `games/{owner}/{id}.json`. `owner` es un hash del `sub` de Google."""

    RECENT_LIMIT = 50

    def __init__(self, objects: ObjectStore) -> None:
        self.objects = objects

    @staticmethod
    def _key(owner: str, game_id: str) -> str:
        if not _HEX.match(owner) or not _HEX.match(game_id):
            raise ValueError("Identificador no válido")
        return f"games/{owner}/{game_id}.json"

    def save(self, game: GameRecord) -> None:
        data = {k: v for k, v in asdict(game).items() if k != "etag"}
        raw = json.dumps(data, ensure_ascii=False, indent=1).encode("utf-8")
        key = self._key(game.owner, game.id)
        new_etag = self.objects.write(key, raw, etag=game.etag)
        if new_etag is None:
            raise GameConflictError(game.id)
        game.etag = new_etag

    def get(self, owner: str, game_id: str) -> GameRecord | None:
        if not _HEX.match(game_id or ""):
            return None
        found = self.objects.read(self._key(owner, game_id))
        if found is None:
            return None
        raw, etag = found
        game = GameRecord(**json.loads(raw.decode("utf-8")))
        if game.owner != owner or game.id != game_id:  # defensa en profundidad
            return None
        game.etag = etag
        return game

    def recent(self, owner: str, limit: int = RECENT_LIMIT) -> list[GameRecord]:
        """Las partidas más recientes del usuario (como mucho `limit`, para acotar lecturas)."""
        if not _HEX.match(owner):
            raise ValueError("Identificador no válido")
        listed = sorted(self.objects.list(f"games/{owner}"), key=lambda kv: kv[1], reverse=True)[:limit]
        games = []
        for key, _ in listed:
            game = self.get(owner, key.rsplit("/", 1)[-1].removesuffix(".json"))
            if game:
                games.append(game)
        return sorted(games, key=lambda g: g.created, reverse=True)

    def previous_patterns(self, owner: str, exclude_id: str) -> list[list[str]]:
        return [g.patrones for g in self.recent(owner) if g.id != exclude_id and g.analysis is not None]


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
