from __future__ import annotations

import chess

from meaculpa.llm import MockProvider, ScriptedProvider
from meaculpa.llm_player import LLMOpponent, MetricsStore, parse_proposal


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
