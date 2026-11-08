from __future__ import annotations

import chess
import pytest

from meaculpa.llm import MockProvider, ScriptedProvider
from meaculpa.llm_player import (
    BudgetExceeded,
    CostTracker,
    LLMForfeit,
    LLMOpponent,
    MetricsStore,
    extract_move_text,
    parse_proposal,
)


def test_parse_proposal_accepts_san_and_uci():
    b = chess.Board()
    assert parse_proposal(b, "e4") == (chess.Move.from_uci("e2e4"), None)
    assert parse_proposal(b, "Nf3.") == (chess.Move.from_uci("g1f3"), None)
    assert parse_proposal(b, "g1f3")[0] == chess.Move.from_uci("g1f3")


def test_parse_proposal_rejects_illegal_and_garbage():
    b = chess.Board()
    for bad in ["e5", "Ke9", "Qh5", "", "   ", "hola", "e2e5", None]:
        move, err = parse_proposal(b, bad)
        assert move is None and err


def test_parse_proposal_reports_ambiguity():
    b = chess.Board("4k3/8/8/8/8/5N2/8/1N2K3 w - - 0 1")  # Nd2 puede ser de b1 o de f3
    move, err = parse_proposal(b, "Nd2")
    assert move is None and "ambigua" in err


def test_retry_feeds_error_back_and_succeeds(tmp_path):
    provider = ScriptedProvider(["Ke9", "e5", "e4"])
    metrics = MetricsStore(tmp_path / "m.jsonl")
    record = LLMOpponent(provider, temperature=0.3, max_retries=3, metrics=metrics).play(chess.Board())
    assert record.final_uci == "e2e4"
    assert not record.fallback
    assert [a.valida for a in record.attempts] == [False, False, True]
    assert record.illegal_count == 2
    # El segundo intento recibe la propuesta fallida y el motivo.
    second_call = provider.calls[1]
    assert second_call[-2]["role"] == "assistant"
    assert "Ke9" in second_call[-1]["content"] and "no vale" in second_call[-1]["content"]
    summary = metrics.summary()[0]
    assert summary["propuestas"] == 3 and summary["propuestas_ilegales"] == 2
    assert summary["legal_a_la_primera"] == 0.0 and summary["fallbacks"] == 0


def test_fallback_after_max_retries_is_legal_and_recorded():
    provider = ScriptedProvider([None, "Ke9", "xx", "Qh9", "e4"])
    board = chess.Board()
    record = LLMOpponent(provider, max_retries=3).play(board)
    assert record.fallback
    assert len(record.attempts) == 4  # 1 intento + 3 reintentos; "e4" nunca se llega a pedir
    assert record.attempts[0].error.startswith("respuesta no parseable")
    assert chess.Move.from_uci(record.final_uci) in board.legal_moves


def test_mock_proposes_more_illegal_moves_at_high_temperature():
    def illegal_rate(temp: float) -> float:
        opp = LLMOpponent(MockProvider(seed=11), temperature=temp, max_retries=3)
        board = chess.Board()
        illegal = total = 0
        for _ in range(80):
            if board.is_game_over():
                board = chess.Board()
            rec = opp.play(board)
            illegal += rec.illegal_count
            total += len(rec.attempts)
            board.push_uci(rec.final_uci)
        return illegal / total

    assert illegal_rate(1.5) > illegal_rate(0.0)


# ------------------------------------------------------------ modo experimento y métricas
def test_temperature_is_not_sent_unless_configured():
    provider = ScriptedProvider(["e4", "d4"])
    LLMOpponent(provider).play(chess.Board())
    LLMOpponent(provider, temperature=0.3).play(chess.Board())
    assert provider.temperatures == [None, 0.3]


def test_elo_mode_forfeits_instead_of_greedy_fallback(tmp_path):
    provider = ScriptedProvider(["Ke9", "Qh9", "xx", "e4"])
    metrics = MetricsStore(tmp_path / "m.jsonl")
    opp = LLMOpponent(provider, max_retries=2, elo_mode=True, metrics=metrics)
    record = opp.play(chess.Board())
    assert record.forfeit and not record.fallback and record.final_uci == ""
    assert len(record.attempts) == 3 and record.illegal_count == 3
    with pytest.raises(LLMForfeit):
        LLMOpponent(ScriptedProvider([None, None]), max_retries=1, elo_mode=True).choose_move(chess.Board())
    assert metrics.summary()[0]["derrotas_por_ilegal"] == 1


def test_per_move_metrics_tokens_latency_and_cost():
    provider = ScriptedProvider(["Ke9", "e4"], price_input_per_m=1.0, price_output_per_m=5.0)
    record = LLMOpponent(provider).play(chess.Board())
    d = record.to_dict()
    assert d["intentos"] == 2 and d["illegal_count"] == 1
    assert d["input_tokens"] == 20 and d["output_tokens"] == 10
    assert d["latency_ms"] == 2.0
    assert d["cost_usd"] == pytest.approx(2 * (10 * 1.0 + 5 * 5.0) / 1e6)
    assert d["attempts"][0]["cost_usd"] == pytest.approx((10 + 25) / 1e6)


def test_hard_budget_stops_before_the_call():
    provider = ScriptedProvider(["e4"], price_input_per_m=1000.0, price_output_per_m=1000.0)
    budget = CostTracker(limit_usd=0.01)
    with pytest.raises(BudgetExceeded):
        LLMOpponent(provider, budget=budget, elo_mode=True).play(chess.Board())
    assert provider.calls == [] and budget.spent_usd == 0 and budget.pending_usd == 0


def test_budget_tracks_real_cost_and_parent():
    total = CostTracker(limit_usd=1.0)
    mine = CostTracker(parent=total)
    provider = ScriptedProvider(["e4"], price_input_per_m=1.0, price_output_per_m=1.0)
    LLMOpponent(provider, budget=mine).play(chess.Board())
    assert mine.spent_usd == pytest.approx(15 / 1e6) and total.spent_usd == pytest.approx(15 / 1e6)
    assert total.pending_usd == 0 and mine.pending_usd == 0


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Jugada: Nf3", "Nf3"),
        ("Pienso en e4 o d4...\nJugada: **d4**", "d4"),
        ("Jugada: 12... Bxe5+", "Bxe5+"),
        ('{"move": "O-O"}', "O-O"),
        ("Me gusta 0-0 aquí", "O-O"),
        ("Tras analizar, juego exd5.", "exd5"),
        ("Move: e8=Q", "e8=Q"),
        ("ninguna jugada", None),
        ("", None),
    ],
)
def test_extract_move_text(text, expected):
    assert extract_move_text(text) == expected


def test_text_mode_player_validates_with_python_chess():
    provider = ScriptedProvider(["Creo que Ke9.\nJugada: Ke9", "Vale.\nJugada: e4"], supports_structured=False)
    record = LLMOpponent(provider).play(chess.Board())
    assert record.final_uci == "e2e4" and [a.modo for a in record.attempts] == ["texto", "texto"]
    assert "Jugada:" in provider.calls[0][0]["content"]  # el prompt de sistema pide la línea final
