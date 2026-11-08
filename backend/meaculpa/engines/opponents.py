"""Rivales clásicos: aleatorio, codicioso, minimax alfa-beta y Stockfish (opcional)."""

from __future__ import annotations

import os
import random
import shutil
from dataclasses import dataclass, field
from typing import Any, Protocol

import chess
import chess.engine

from .alphabeta import AlphaBetaSearcher
from .evaluation import PIECE_VALUES


@dataclass
class MoveDecision:
    move: chess.Move
    meta: dict[str, Any] = field(default_factory=dict)


class Opponent(Protocol):
    id: str

    def choose_move(self, board: chess.Board) -> MoveDecision: ...


class RandomOpponent:
    id = "random"

    def __init__(self, seed: int | None = None) -> None:
        self._rng = random.Random(seed)

    def choose_move(self, board: chess.Board) -> MoveDecision:
        return MoveDecision(self._rng.choice(list(board.legal_moves)), {"engine": "aleatorio"})


class GreedyOpponent:
    """Captura lo que más material gane (sin mirar la respuesta). Si no hay capturas, aleatorio.

    Da mate en una si puede: un codicioso que no remata sería demasiado tonto.
    """

    id = "greedy"

    def __init__(self, seed: int | None = None) -> None:
        self._rng = random.Random(seed)

    def choose_move(self, board: chess.Board) -> MoveDecision:
        moves = list(board.legal_moves)
        for m in moves:
            board.push(m)
            mate = board.is_checkmate()
            board.pop()
            if mate:
                return MoveDecision(m, {"engine": "codicioso", "motivo": "mate"})

        def gain(m: chess.Move) -> int:
            g = 0
            if board.is_en_passant(m):
                g += PIECE_VALUES[chess.PAWN]
            elif board.is_capture(m):
                g += PIECE_VALUES[board.piece_type_at(m.to_square) or chess.PAWN]
            if m.promotion:
                g += PIECE_VALUES[m.promotion] - PIECE_VALUES[chess.PAWN]
            return g

        best_gain = max(gain(m) for m in moves)
        if best_gain <= 0:
            return MoveDecision(self._rng.choice(moves), {"engine": "codicioso", "motivo": "sin capturas"})
        candidates = [m for m in moves if gain(m) == best_gain]
        return MoveDecision(self._rng.choice(candidates), {"engine": "codicioso", "motivo": f"gana {best_gain} cp"})


class MinimaxOpponent:
    id = "minimax"

    def __init__(self, depth: int = 3) -> None:
        self.depth = max(1, min(depth, 5))

    def choose_move(self, board: chess.Board) -> MoveDecision:
        res = AlphaBetaSearcher(depth=self.depth).search(board)
        assert res.move is not None
        return MoveDecision(
            res.move,
            {"engine": "minimax alfa-beta", "profundidad": res.depth, "nodos": res.nodes, "eval_cp": res.score},
        )


def find_stockfish() -> str | None:
    """Stockfish solo si ya está instalado: STOCKFISH_PATH o `stockfish` en el PATH."""
    env = os.environ.get("STOCKFISH_PATH")
    if env and os.path.isfile(env):
        return env
    return shutil.which("stockfish")


class StockfishOpponent:
    id = "stockfish"

    def __init__(self, path: str, skill: int = 5, movetime: float = 0.2) -> None:
        self.path = path
        self.skill = max(0, min(skill, 20))
        self.movetime = movetime

    def choose_move(self, board: chess.Board) -> MoveDecision:
        with chess.engine.SimpleEngine.popen_uci(self.path) as engine:
            engine.configure({"Skill Level": self.skill})
            result = engine.play(board, chess.engine.Limit(time=self.movetime))
        assert result.move is not None
        return MoveDecision(result.move, {"engine": "stockfish", "skill": self.skill})


def classic_opponent_catalog() -> list[dict[str, Any]]:
    sf = find_stockfish()
    catalog: list[dict[str, Any]] = [
        {"id": "random", "nombre": "Aleatorio", "descripcion": "Mueve cualquier jugada legal al azar.", "nivel": 1},
        {
            "id": "greedy",
            "nombre": "Codicioso",
            "descripcion": "Captura la pieza más valiosa que pueda, sin pensar en la respuesta.",
            "nivel": 2,
        },
        {
            "id": "minimax",
            "nombre": "Minimax alfa-beta",
            "descripcion": "Busca N plies con poda alfa-beta, quiescencia y tablas pieza-casilla.",
            "nivel": 3,
            "parametros": {"profundidad": {"min": 1, "max": 4, "defecto": 3}},
        },
    ]
    if sf:
        catalog.append(
            {
                "id": "stockfish",
                "nombre": "Stockfish",
                "descripcion": "Motor de verdad, con nivel de habilidad 0-20.",
                "nivel": 4,
                "parametros": {"skill": {"min": 0, "max": 20, "defecto": 5}},
            }
        )
    return catalog
