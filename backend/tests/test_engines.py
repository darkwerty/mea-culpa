from __future__ import annotations

import chess
import pytest

from meaculpa.engines import (
    AlphaBetaSearcher,
    GreedyOpponent,
    MinimaxOpponent,
    RandomOpponent,
    evaluate,
    is_mate_score,
    mate_in_moves,
)

BACK_RANK_MATE_IN_1 = "6k1/5ppp/8/8/8/8/5PPP/R5K1 w - - 0 1"  # Ra8#
ROOK_LADDER_MATE_IN_2 = "7k/8/8/8/8/8/R7/1R4K1 w - - 0 1"  # 1.Rb7 Kg8 2.Ra8#
SCHOLAR_MATE_IN_1 = "r1bqkb1r/pppp1ppp/2n2n2/4p2Q/2B1P3/8/PPPP1PPP/RNB1K1NR w KQkq - 4 4"  # Qxf7#


def _is_mate_in_one(board: chess.Board) -> bool:
    for m in board.legal_moves:
        board.push(m)
        mate = board.is_checkmate()
        board.pop()
        if mate:
            return True
    return False


def test_evaluation_is_symmetric():
    assert evaluate(chess.Board()) == 0
    b = chess.Board("r1bqkbnr/pppp1ppp/2n5/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R w KQkq - 2 3")
    assert evaluate(b) == -evaluate(b.mirror())


@pytest.mark.parametrize("depth", [1, 2, 3])
@pytest.mark.parametrize("fen", [BACK_RANK_MATE_IN_1, SCHOLAR_MATE_IN_1])
def test_alphabeta_finds_mate_in_one(fen, depth):
    board = chess.Board(fen)
    res = AlphaBetaSearcher(depth=depth).search(board)
    board.push(res.move)
    assert board.is_checkmate()
    assert is_mate_score(res.score) and mate_in_moves(res.score) == 1


def test_alphabeta_finds_mate_in_two():
    board = chess.Board(ROOK_LADDER_MATE_IN_2)
    assert not _is_mate_in_one(board)  # de verdad es mate en 2, no en 1
    res = AlphaBetaSearcher(depth=3).search(board)
    assert mate_in_moves(res.score) == 2
    board.push(res.move)
    replies = list(board.legal_moves)
    assert replies, "la primera jugada no puede dar ahogado"
    for reply in replies:  # cualquier defensa permite mate en 1
        board.push(reply)
        assert _is_mate_in_one(board)
        board.pop()


def test_transposition_table_does_not_change_the_score():
    board = chess.Board("r1b1k1nr/p2p1ppp/1bn5/1p2P1q1/2BP4/2P2N2/P4PPP/RNBQ1RK1 w kq - 1 10")
    a = AlphaBetaSearcher(depth=3, use_tt=False).search(board)
    b = AlphaBetaSearcher(depth=3, use_tt=True).search(board)
    assert a.score == b.score


def test_minimax_saves_attacked_queen():
    # La dama blanca de d4 está atacada por el peón de e5 (tras ...e5) y no puede quedarse ahí.
    board = chess.Board("rnbqkbnr/pppp1ppp/8/4p3/3Q4/8/PPP1PPPP/RNB1KBNR w KQkq - 0 3")
    move = MinimaxOpponent(depth=2).choose_move(board).move
    board.push(move)
    queen_sq = next(iter(board.pieces(chess.QUEEN, chess.WHITE)))
    assert not board.attackers(chess.BLACK, queen_sq) or board.attackers(chess.WHITE, queen_sq)


def test_greedy_takes_the_queen():
    board = chess.Board("4k3/8/8/3q4/8/8/3R4/4K3 w - - 0 1")
    assert GreedyOpponent(seed=1).choose_move(board).move == chess.Move.from_uci("d2d5")


def test_greedy_mates_when_possible():
    board = chess.Board(BACK_RANK_MATE_IN_1)
    board.push(GreedyOpponent(seed=1).choose_move(board).move)
    assert board.is_checkmate()


def test_random_is_always_legal():
    opp = RandomOpponent(seed=3)
    board = chess.Board()
    for _ in range(60):
        if board.is_game_over():
            break
        move = opp.choose_move(board).move
        assert move in board.legal_moves
        board.push(move)
