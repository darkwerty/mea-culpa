"""Minimax (negamax) con poda alfa-beta, profundización iterativa y quiescencia.

Diseño deliberadamente pequeño y legible:
- negamax con ventana alfa-beta;
- ordenación de jugadas: jugada principal de la iteración anterior, capturas
  MVV-LVA, promociones, killer moves y el resto;
- búsqueda de quiescencia sobre capturas para evitar el efecto horizonte;
- tabla de transposiciones (opcionalmente compartida entre búsquedas, útil al analizar
  una partida entera, donde posiciones consecutivas comparten subárboles);
- puntuaciones de mate = MATE - ply, así se prefiere el mate más corto.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import chess

from .evaluation import PIECE_VALUES, evaluate_stm

MATE = 100_000
MATE_THRESHOLD = MATE - 1_000
MAX_QDEPTH = 6
TT_MAX_ENTRIES = 400_000
EXACT, LOWER, UPPER = 0, 1, 2


def is_mate_score(score: int) -> bool:
    return abs(score) >= MATE_THRESHOLD


def mate_in_moves(score: int) -> int:
    """Número de jugadas (no plies) hasta el mate. Positivo = gana quien mueve."""
    plies = MATE - abs(score)
    moves = (plies + 1) // 2
    return moves if score > 0 else -moves


@dataclass
class SearchResult:
    move: chess.Move | None
    score: int  # desde el punto de vista del bando que mueve
    depth: int
    nodes: int
    pv: list[chess.Move] = field(default_factory=list)


class AlphaBetaSearcher:
    def __init__(self, depth: int = 3, quiescence: bool = True, use_tt: bool = True) -> None:
        if depth < 1:
            raise ValueError("depth debe ser >= 1")
        self.depth = depth
        self.quiescence = quiescence
        self.use_tt = use_tt
        self.nodes = 0
        # clave -> (profundidad, tipo, puntuación, jugada)
        self._tt: dict[object, tuple[int, int, int, chess.Move | None]] = {}
        self._killers: dict[int, list[chess.Move]] = {}
        self._pv_move: dict[int, chess.Move] = {}

    # ------------------------------------------------------------------ orden
    def _order(self, board: chess.Board, moves: list[chess.Move], ply: int) -> list[chess.Move]:
        pv = self._pv_move.get(ply)
        killers = self._killers.get(ply, [])

        def key(m: chess.Move) -> int:
            if pv is not None and m == pv:
                return -1_000_000
            k = 0
            if board.is_capture(m):
                victim = board.piece_type_at(m.to_square) or chess.PAWN  # en passant
                attacker = board.piece_type_at(m.from_square) or chess.PAWN
                k -= 10_000 + 10 * PIECE_VALUES[victim] - PIECE_VALUES[attacker] // 10
            if m.promotion:
                k -= 9_000 + PIECE_VALUES[m.promotion]
            if m in killers:
                k -= 5_000
            return k

        return sorted(moves, key=key)

    # ------------------------------------------------------------- búsqueda
    def search(self, board: chess.Board, depth: int | None = None) -> SearchResult:
        depth = depth or self.depth
        board = board.copy(stack=False)
        if len(self._tt) > TT_MAX_ENTRIES:
            self._tt.clear()
        self.nodes = 0
        self._killers.clear()
        self._pv_move.clear()
        result = SearchResult(move=None, score=0, depth=0, nodes=0)
        for d in range(1, depth + 1):
            pv: list[chess.Move] = []
            score = self._negamax(board, d, -MATE - 1, MATE + 1, 0, pv)
            result = SearchResult(move=pv[0] if pv else None, score=score, depth=d, nodes=self.nodes, pv=list(pv))
            self._pv_move = {i: m for i, m in enumerate(pv)}
            if is_mate_score(score) and score > 0:
                break  # mate encontrado: no hace falta profundizar
        if result.move is None:
            legal = list(board.legal_moves)
            result.move = legal[0] if legal else None
        return result

    def _negamax(self, board: chess.Board, depth: int, alpha: int, beta: int, ply: int, pv: list[chess.Move]) -> int:
        self.nodes += 1
        if ply > 0 and (board.halfmove_clock >= 100 or board.is_insufficient_material()):
            return 0
        if depth <= 0:
            if board.is_check() and not any(board.legal_moves):
                return -MATE + ply
            return self._quiesce(board, alpha, beta, ply, 0) if self.quiescence else evaluate_stm(board)

        moves = list(board.legal_moves)
        if not moves:
            return -MATE + ply if board.is_check() else 0

        key = board._transposition_key() if self.use_tt else None  # noqa: SLF001
        tt_move = None
        if key is not None and key in self._tt:
            tt_depth, flag, tt_score, tt_move = self._tt[key]
            if ply > 0 and tt_depth >= depth:
                tt_score = _from_tt(tt_score, ply)
                if flag == EXACT or (flag == LOWER and tt_score >= beta) or (flag == UPPER and tt_score <= alpha):
                    if tt_move is not None:
                        pv[:] = [tt_move]
                    return tt_score

        alpha_orig = alpha
        best = -MATE - 1
        best_move: chess.Move | None = None
        ordered = self._order(board, moves, ply)
        if tt_move is not None and tt_move in moves and ply not in self._pv_move:
            ordered.remove(tt_move)
            ordered.insert(0, tt_move)
        for move in ordered:
            child_pv: list[chess.Move] = []
            board.push(move)
            score = -self._negamax(board, depth - 1, -beta, -alpha, ply + 1, child_pv)
            board.pop()
            if score > best:
                best = score
                best_move = move
            if score > alpha:
                alpha = score
                pv[:] = [move, *child_pv]
            if alpha >= beta:
                if not board.is_capture(move):
                    ks = self._killers.setdefault(ply, [])
                    if move not in ks:
                        ks.insert(0, move)
                        del ks[2:]
                break
        if key is not None:
            flag = UPPER if best <= alpha_orig else LOWER if best >= beta else EXACT
            self._tt[key] = (depth, flag, _to_tt(best, ply), best_move)
        return best

    def _quiesce(self, board: chess.Board, alpha: int, beta: int, ply: int, qdepth: int) -> int:
        self.nodes += 1
        stand_pat = evaluate_stm(board)
        if stand_pat >= beta:
            return stand_pat
        if stand_pat > alpha:
            alpha = stand_pat
        if qdepth >= MAX_QDEPTH:
            return stand_pat
        captures = self._order(board, list(board.generate_legal_captures()), -1)
        for move in captures:
            board.push(move)
            score = -self._quiesce(board, -beta, -alpha, ply + 1, qdepth + 1)
            board.pop()
            if score >= beta:
                return score
            if score > alpha:
                alpha = score
        return alpha


def _to_tt(score: int, ply: int) -> int:
    """Las puntuaciones de mate se guardan relativas al nodo, no a la raíz."""
    if score >= MATE_THRESHOLD:
        return score + ply
    if score <= -MATE_THRESHOLD:
        return score - ply
    return score


def _from_tt(score: int, ply: int) -> int:
    if score >= MATE_THRESHOLD:
        return score - ply
    if score <= -MATE_THRESHOLD:
        return score + ply
    return score


def best_move(board: chess.Board, depth: int = 3) -> SearchResult:
    return AlphaBetaSearcher(depth=depth).search(board)
