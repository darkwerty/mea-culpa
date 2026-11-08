"""Stockfish como ancla: proceso persistente, opciones UCI y límite constante (con un motor falso)."""

from __future__ import annotations

import chess
import chess.engine
import pytest

from meaculpa.engines import StockfishAnchor, find_stockfish


class FakeEngine:
    instances: list[FakeEngine] = []

    def __init__(self, path: str) -> None:
        self.path = path
        self.options: dict = {}
        self.limits: list[chess.engine.Limit] = []
        self.quit_called = False
        FakeEngine.instances.append(self)

    def configure(self, options: dict) -> None:
        self.options.update(options)

    def play(self, board: chess.Board, limit: chess.engine.Limit):
        self.limits.append(limit)
        return chess.engine.PlayResult(next(iter(board.legal_moves)), None)

    def quit(self) -> None:
        self.quit_called = True


@pytest.fixture(autouse=True)
def _reset():
    FakeEngine.instances.clear()


def test_one_process_per_game_with_limited_strength():
    anchor = StockfishAnchor("sf.exe", uci_elo=1468, movetime=0.05, engine_factory=FakeEngine)
    with anchor:
        board = chess.Board()
        for _ in range(4):
            board.push(anchor.choose_move(board).move)
    assert len(FakeEngine.instances) == 1  # un solo proceso para toda la partida
    eng = FakeEngine.instances[0]
    assert eng.options == {"Threads": 1, "Hash": 16, "UCI_LimitStrength": True, "UCI_Elo": 1468}
    assert all(lim.time == 0.05 and lim.nodes is None for lim in eng.limits)
    assert eng.quit_called


def test_node_limit_and_full_strength():
    anchor = StockfishAnchor("sf.exe", uci_elo=None, nodes=5000, hash_mb=8, engine_factory=FakeEngine)
    anchor.choose_move(chess.Board())
    eng = FakeEngine.instances[0]
    assert eng.limits[0].nodes == 5000 and eng.limits[0].time is None
    assert "UCI_LimitStrength" not in eng.options and eng.options["Hash"] == 8
    anchor.close()


def test_uci_elo_below_minimum_is_rejected():
    with pytest.raises(ValueError):
        StockfishAnchor("sf.exe", uci_elo=1000, engine_factory=FakeEngine)


def test_path_comes_from_env(monkeypatch, tmp_path):
    exe = tmp_path / "stockfish.exe"
    exe.write_text("")
    monkeypatch.setenv("STOCKFISH_PATH", str(exe))
    assert find_stockfish() == str(exe)
    assert StockfishAnchor(engine_factory=FakeEngine).path == str(exe)


@pytest.mark.skipif(find_stockfish() is None, reason="Stockfish no instalado (STOCKFISH_PATH)")
def test_real_stockfish_plays_legal_moves():
    with StockfishAnchor(uci_elo=1320, nodes=2000) as anchor:
        board = chess.Board()
        for _ in range(6):
            move = anchor.choose_move(board).move
            assert move in board.legal_moves
            board.push(move)
