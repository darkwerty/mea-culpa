"""Posiciones iniciales sacadas de partidas reales: sin repetir, colores alternos y sin nombres de rivales."""

from __future__ import annotations

import chess
import pytest

from meaculpa.openings import assign_openings, extract_start_positions, read_games

PGN = """[Event "Live Chess"]
[Site "Chess.com"]
[Date "2026.10.30"]
[White "qwerty0x"]
[Black "NombreRivalUno"]
[Result "0-1"]
[WhiteElo "1094"]
[BlackElo "1099"]
[ECO "B00"]

1. e4 {[%clk 0:02:59.1]} b6 2. d4 Bb7 3. Nf3 Bxe4 4. Nc3 Bb7 5. Bf4 d6 6. d5 Nf6 0-1

[Event "Live Chess"]
[Site "Chess.com"]
[Date "2026.10.29"]
[White "NombreRivalDos"]
[Black "qwerty0x"]
[Result "1-0"]
[WhiteElo "1150"]
[BlackElo "1090"]
[ECO "C50"]

1. e4 e5 2. Nf3 Nc6 3. Bc4 Bc5 4. c3 Nf6 5. d4 exd4 1-0

[Event "Live Chess"]
[White "qwerty0x"]
[Black "NombreRivalTres"]
[Result "1-0"]
[WhiteElo "1100"]
[BlackElo "1120"]

1. e4 e5 2. Nf3 Nc6 3. Bc4 Bc5 4. c3 Nf6 5. d4 exd4 1-0

[Event "Corta"]
[White "qwerty0x"]
[Black "NombreRivalCuatro"]
[Result "1-0"]
[WhiteElo "1100"]
[BlackElo "800"]

1. f3 e5 2. g4 Qh4# 0-1
"""


def test_unique_positions_in_range_without_rival_names():
    games = read_games(PGN)
    assert len(games) == 4
    positions = extract_start_positions(games, min_plies=4, max_plies=8, seed=3, user="qwerty0x")
    epds = [chess.Board(p.fen).epd() for p in positions]
    assert len(epds) == len(set(epds))  # sin repetir (las partidas 2 y 3 son idénticas: o se descarta una o
    # se toma en otro ply)
    for p in positions:
        assert 4 <= p.plies <= 8
        assert p.board().fen() == p.fen and len(p.board().move_stack) == p.plies
        assert "NombreRival" not in p.origen and "rival (Elo" in p.origen
        assert not p.board().is_game_over()
    # La partida de 4 plies (mate del loco) no deja partida después: no aporta posición.
    assert all("800" not in p.origen for p in positions)


def test_evaluator_filters_unbalanced_positions():
    games = read_games(PGN)
    positions = extract_start_positions(games, evaluator=lambda b: 500, max_abs_cp=150)
    assert positions == []


def test_assign_openings_alternates_colors_and_pairs_positions():
    positions = extract_start_positions(read_games(PGN), seed=1)
    plan = assign_openings(positions, 4, both_colors=True)
    assert [white for _, white in plan] == [True, False, True, False]
    assert plan[0][0] == plan[1][0] and plan[2][0] == plan[3][0] and plan[0][0] != plan[2][0]
    single = assign_openings(positions, 3, both_colors=False)
    assert len({p.id for p, _ in single}) == 3 and [w for _, w in single] == [True, False, True]
    with pytest.raises(ValueError):
        assign_openings(positions, 2 * len(positions) + 1, both_colors=True)
