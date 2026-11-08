"""Evaluación estática: material + tablas pieza-casilla (PST).

Las tablas son las de la "Simplified Evaluation Function" de Tomasz Michniewski
(chessprogramming.org). Están escritas como se ve el tablero desde las blancas:
la primera fila es la 8ª fila. Todas las puntuaciones en centipeones.
"""

from __future__ import annotations

import chess

PIECE_VALUES = {
    chess.PAWN: 100,
    chess.KNIGHT: 320,
    chess.BISHOP: 330,
    chess.ROOK: 500,
    chess.QUEEN: 900,
    chess.KING: 0,
}

# fmt: off
_PAWN = [
     0,   0,   0,   0,   0,   0,   0,   0,
    50,  50,  50,  50,  50,  50,  50,  50,
    10,  10,  20,  30,  30,  20,  10,  10,
     5,   5,  10,  25,  25,  10,   5,   5,
     0,   0,   0,  20,  20,   0,   0,   0,
     5,  -5, -10,   0,   0, -10,  -5,   5,
     5,  10,  10, -20, -20,  10,  10,   5,
     0,   0,   0,   0,   0,   0,   0,   0,
]
_KNIGHT = [
   -50, -40, -30, -30, -30, -30, -40, -50,
   -40, -20,   0,   0,   0,   0, -20, -40,
   -30,   0,  10,  15,  15,  10,   0, -30,
   -30,   5,  15,  20,  20,  15,   5, -30,
   -30,   0,  15,  20,  20,  15,   0, -30,
   -30,   5,  10,  15,  15,  10,   5, -30,
   -40, -20,   0,   5,   5,   0, -20, -40,
   -50, -40, -30, -30, -30, -30, -40, -50,
]
_BISHOP = [
   -20, -10, -10, -10, -10, -10, -10, -20,
   -10,   0,   0,   0,   0,   0,   0, -10,
   -10,   0,   5,  10,  10,   5,   0, -10,
   -10,   5,   5,  10,  10,   5,   5, -10,
   -10,   0,  10,  10,  10,  10,   0, -10,
   -10,  10,  10,  10,  10,  10,  10, -10,
   -10,   5,   0,   0,   0,   0,   5, -10,
   -20, -10, -10, -10, -10, -10, -10, -20,
]
_ROOK = [
     0,   0,   0,   0,   0,   0,   0,   0,
     5,  10,  10,  10,  10,  10,  10,   5,
    -5,   0,   0,   0,   0,   0,   0,  -5,
    -5,   0,   0,   0,   0,   0,   0,  -5,
    -5,   0,   0,   0,   0,   0,   0,  -5,
    -5,   0,   0,   0,   0,   0,   0,  -5,
    -5,   0,   0,   0,   0,   0,   0,  -5,
     0,   0,   0,   5,   5,   0,   0,   0,
]
_QUEEN = [
   -20, -10, -10,  -5,  -5, -10, -10, -20,
   -10,   0,   0,   0,   0,   0,   0, -10,
   -10,   0,   5,   5,   5,   5,   0, -10,
    -5,   0,   5,   5,   5,   5,   0,  -5,
     0,   0,   5,   5,   5,   5,   0,  -5,
   -10,   5,   5,   5,   5,   5,   0, -10,
   -10,   0,   5,   0,   0,   0,   0, -10,
   -20, -10, -10,  -5,  -5, -10, -10, -20,
]
_KING_MG = [
   -30, -40, -40, -50, -50, -40, -40, -30,
   -30, -40, -40, -50, -50, -40, -40, -30,
   -30, -40, -40, -50, -50, -40, -40, -30,
   -30, -40, -40, -50, -50, -40, -40, -30,
   -20, -30, -30, -40, -40, -30, -30, -20,
   -10, -20, -20, -20, -20, -20, -20, -10,
    20,  20,   0,   0,   0,   0,  20,  20,
    20,  30,  10,   0,   0,  10,  30,  20,
]
_KING_EG = [
   -50, -40, -30, -20, -20, -30, -40, -50,
   -30, -20, -10,   0,   0, -10, -20, -30,
   -30, -10,  20,  30,  30,  20, -10, -30,
   -30, -10,  30,  40,  40,  30, -10, -30,
   -30, -10,  30,  40,  40,  30, -10, -30,
   -30, -10,  20,  30,  30,  20, -10, -30,
   -30, -30,   0,   0,   0,   0, -30, -30,
   -50, -30, -30, -30, -30, -30, -30, -50,
]
# fmt: on

_PST = {
    chess.PAWN: _PAWN,
    chess.KNIGHT: _KNIGHT,
    chess.BISHOP: _BISHOP,
    chess.ROOK: _ROOK,
    chess.QUEEN: _QUEEN,
}

# Precalcula tablas indexadas por casilla de python-chess (a1 = 0) para cada color.
# Para blancas la casilla a1 corresponde al índice 56 de la tabla "visual".
_TABLES: dict[tuple[int, bool], list[int]] = {}
for _pt, _table in [*_PST.items(), (-1, _KING_MG), (-2, _KING_EG)]:
    _TABLES[(_pt, chess.WHITE)] = [_table[chess.square_mirror(sq)] for sq in chess.SQUARES]
    _TABLES[(_pt, chess.BLACK)] = [_table[sq] for sq in chess.SQUARES]


def is_endgame(board: chess.Board) -> bool:
    """Final si no quedan damas o si cada bando tiene poco material de piezas."""
    if not board.queens:
        return True
    for color in chess.COLORS:
        minor_major = (
            len(board.pieces(chess.KNIGHT, color)) * 320
            + len(board.pieces(chess.BISHOP, color)) * 330
            + len(board.pieces(chess.ROOK, color)) * 500
        )
        if board.pieces(chess.QUEEN, color) and minor_major > 330:
            return False
    return True


def material(board: chess.Board, color: chess.Color) -> int:
    return sum(
        len(board.pieces(pt, color)) * PIECE_VALUES[pt]
        for pt in (chess.PAWN, chess.KNIGHT, chess.BISHOP, chess.ROOK, chess.QUEEN)
    )


def evaluate(board: chess.Board) -> int:
    """Evaluación desde el punto de vista de las blancas (centipeones)."""
    score = 0
    king_key = -2 if is_endgame(board) else -1
    for color in chess.COLORS:
        sign = 1 if color == chess.WHITE else -1
        for pt in (chess.PAWN, chess.KNIGHT, chess.BISHOP, chess.ROOK, chess.QUEEN):
            table = _TABLES[(pt, color)]
            value = PIECE_VALUES[pt]
            for sq in chess.scan_forward(board.pieces_mask(pt, color)):
                score += sign * (value + table[sq])
        king_sq = board.king(color)
        if king_sq is not None:
            score += sign * _TABLES[(king_key, color)][king_sq]
    return score


def evaluate_stm(board: chess.Board) -> int:
    """Evaluación desde el punto de vista del bando al que le toca mover."""
    score = evaluate(board)
    return score if board.turn == chess.WHITE else -score
