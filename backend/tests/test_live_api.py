"""Barra de evaluación (/eval), pistas (/hint), deshacer (/takeback) y partidas «con ayuda» fuera del ranking."""

from __future__ import annotations

import dataclasses
import os
from pathlib import Path

import chess
import pytest
from conftest import auth, fake_verifier
from fastapi.testclient import TestClient

from meaculpa.auth import owner_id
from meaculpa.hints import clean_text, fallback_explanation, motives
from meaculpa.live import EngineFailure, EvalRateLimiter, Line, LiveEngine, terminal_payload
from meaculpa.llm import MockProvider
from meaculpa.main import create_app

API = "/api/meaculpa"
FOOLS_MATE = "rnb1kbnr/pppp1ppp/8/4p3/6Pq/5P2/PPPPP2P/RNBQKBNR w KQkq - 1 3"  # blancas mateadas
STALEMATE = "7k/5Q2/6K1/8/8/8/8/8 b - - 0 1"
SCHOLAR = "r1bqkb1r/pppp1ppp/2n2n2/4p2Q/2B1P3/8/PPPP1PPP/RNB1K1NR w KQkq - 4 4"  # Qxf7#


class FakeEngine:
    """Motor de pruebas: la primera jugada legal ordenada por UCI y +0,25 para quien mueve."""

    name = "falso"

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.calls = 0

    def analyse(self, board, *, time_s=0.2, depth=None, multipv=1):
        self.calls += 1
        if self.fail:
            raise EngineFailure("caído")
        moves = sorted(board.legal_moves, key=lambda m: m.uci())
        sign = 1 if board.turn == chess.WHITE else -1
        lines = [Line(cp=sign * 25, mate=None, pv=[moves[0]], depth=12)]
        if multipv > 1 and len(moves) > 1:
            lines.append(Line(cp=sign * -300, mate=None, pv=[moves[1]], depth=12))
        return lines

    def close(self):
        pass


class BrokenProvider:
    name, kind = "roto", "test"

    def structured(self, messages, schema, temperature=None):
        raise ConnectionError("el proveedor no responde")


def _client(settings, *, provider=None, engine=None, user="ana") -> TestClient:
    app = create_app(
        settings=settings,
        provider=provider or MockProvider(seed=5),
        token_verifier=fake_verifier,
        live_engine=engine if engine is not None else FakeEngine(),
    )
    return TestClient(app, headers=auth(user) if user else None)


def _new_game(client, color="white", opponent="random"):
    res = client.post(f"{API}/games", json={"opponent": {"type": opponent}, "user_color": color})
    assert res.status_code == 200, res.text
    return res.json()


# ------------------------------------------------------------------ /eval
def test_eval_rejects_invalid_fen(settings):
    client = _client(settings)
    assert client.post(f"{API}/eval", json={"fen": "esto no es un fen válido"}).status_code == 422
    # Bien formado pero imposible: el bando que no mueve está en jaque.
    illegal = "4k3/8/8/8/8/8/4R3/4K3 w - - 0 1"
    assert client.post(f"{API}/eval", json={"fen": illegal}).status_code == 422
    assert client.post(f"{API}/eval", json={"fen": "x" * 200}).status_code == 422


def test_eval_requires_login(settings):
    client = _client(settings, user=None)
    assert client.post(f"{API}/eval", json={"fen": chess.STARTING_FEN}).status_code == 401


def test_eval_checkmate_and_stalemate_without_engine(settings):
    engine = FakeEngine()
    client = _client(settings, engine=engine)
    mate = client.post(f"{API}/eval", json={"fen": FOOLS_MATE}).json()
    assert mate == {
        "cp": None,
        "mate": 0,
        "best_uci": None,
        "best_san": None,
        "pv_san": [],
        "depth": 0,
        "resultado": "0-1",
        "fin": "jaque mate",
    }
    draw = client.post(f"{API}/eval", json={"fen": STALEMATE}).json()
    assert draw["cp"] == 0 and draw["mate"] is None and draw["resultado"] == "1/2-1/2" and draw["fin"] == "rey ahogado"
    assert engine.calls == 0


def test_eval_normal_position_is_from_white(settings):
    client = _client(settings)
    body = client.post(f"{API}/eval", json={"fen": chess.STARTING_FEN}).json()
    assert set(body) >= {"cp", "mate", "best_uci", "best_san", "pv_san", "depth"}
    assert body["cp"] == 25 and body["mate"] is None
    # La mejor jugada no sale por /eval (sería una pista gratis sin marcar la partida «con ayuda»).
    assert body["best_uci"] is None and body["best_san"] is None and body["pv_san"] == []
    black = client.post(f"{API}/eval", json={"fen": "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1"})
    assert black.json()["cp"] == -25  # +0,25 para las negras = -25 desde las blancas


def test_eval_with_the_builtin_fallback_engine(settings):
    client = _client(settings, engine=LiveEngine(path="", size=1, fallback_depth=2))
    body = client.post(f"{API}/eval", json={"fen": SCHOLAR}).json()
    assert body["mate"] == 1 and body["cp"] is None and body["best_san"] is None and body["pv_san"] == []
    black_mates = "rnbqkbnr/pppp1ppp/8/4p3/6P1/5P2/PPPPP2P/RNBQKBNR b KQkq - 0 2"
    body = client.post(f"{API}/eval", json={"fen": black_mates}).json()
    assert body["mate"] == -1 and body["best_uci"] is None


def test_eval_engine_failure_is_503(settings):
    client = _client(settings, engine=FakeEngine(fail=True))
    assert client.post(f"{API}/eval", json={"fen": chess.STARTING_FEN}).status_code == 503


def test_eval_rate_limit(settings):
    client = _client(dataclasses.replace(settings, eval_interval_ms=60_000))
    codes = [client.post(f"{API}/eval", json={"fen": chess.STARTING_FEN}).status_code for _ in range(10)]
    assert codes[:8] == [200] * 8 and codes[8:] == [429, 429]
    # Otra persona tiene su propio cubo.
    other = client.post(f"{API}/eval", json={"fen": chess.STARTING_FEN}, headers=auth("bea"))
    assert other.status_code == 200


def test_eval_rate_limiter_refills_and_has_a_daily_cap():
    now = {"t": 0.0}
    limiter = EvalRateLimiter(0.15, per_day=12, burst=2, clock=lambda: now["t"], wall=lambda: 1_700_000_000.0)
    assert [limiter.allow("a"), limiter.allow("a"), limiter.allow("a")] == [None, None, "rafaga"]
    now["t"] += 0.15
    assert limiter.allow("a") is None
    for _ in range(20):
        now["t"] += 1
        limiter.allow("a")
    now["t"] += 1
    assert limiter.allow("a") == "diario"


STOCKFISH = os.environ.get("STOCKFISH_PATH") or next(
    (
        str(p)
        for p in (Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Packages").glob(
            "Stockfish*/stockfish/stockfish*.exe"
        )
    ),
    None,
)


@pytest.mark.skipif(not STOCKFISH or not Path(STOCKFISH).is_file(), reason="Stockfish no instalado")
def test_real_stockfish_pool_is_reused_and_closed():
    engine = LiveEngine(STOCKFISH, size=1)
    try:
        lines = engine.analyse(chess.Board(SCHOLAR), time_s=0.2, depth=12)
        assert lines[0].mate == 1 and lines[0].pv[0].uci() == "h5f7"
        lines = engine.analyse(chess.Board(), time_s=0.1, depth=10, multipv=2)
        assert len(lines) == 2 and lines[0].cp is not None
        assert len(engine._all) == 1  # el mismo proceso para las dos peticiones
    finally:
        engine.close()
    assert all(slot.engine is None for slot in engine._all)


# ------------------------------------------------------------------ /hint
def test_hint_on_your_turn_marks_the_game(settings):
    client = _client(settings)
    game = _new_game(client)
    res = client.post(f"{API}/games/{game['id']}/hint")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["best_uci"] == "a2a3" and body["best_san"] == "a3" and body["pv_san"] == ["a3"]
    assert body["eval_antes"] == {"cp": 25, "mate": None}
    assert body["eval_despues"] == {"cp": -25, "mate": None}
    assert body["motivos"] and body["explicacion"] and body["fuente"] == "llm"
    assert body["con_ayuda"] is True and body["ayudas"] == {"pistas": 1, "deshacer": 0}
    for ch in "·—–":
        assert ch not in body["explicacion"]
    state = client.get(f"{API}/games/{game['id']}").json()
    assert state["con_ayuda"] is True and state["ayudas"] == {"pistas": 1, "deshacer": 0}
    assert client.get(f"{API}/games").json()[0]["con_ayuda"] is True


def test_new_game_has_no_help(settings):
    client = _client(settings)
    game = _new_game(client)
    assert game["con_ayuda"] is False and game["ayudas"] == {"pistas": 0, "deshacer": 0}


def test_hint_not_your_turn(settings):
    client = _client(settings)
    game = _new_game(client)
    store, owner = client.app.state.store, owner_id("ana")
    record = store.get(owner, game["id"])
    record.moves = ["e2e4"]  # ahora mueven las negras (el rival)
    store.save(record)
    res = client.post(f"{API}/games/{game['id']}/hint")
    assert res.status_code == 409 and "turno" in res.json()["detail"]
    assert client.get(f"{API}/games/{game['id']}").json()["con_ayuda"] is False


def test_hint_on_finished_game(settings):
    client = _client(settings)
    game = _new_game(client)
    client.post(f"{API}/games/{game['id']}/resign")
    assert client.post(f"{API}/games/{game['id']}/hint").status_code == 409


def test_hint_quota(settings):
    client = _client(dataclasses.replace(settings, quota_hints_per_day=2))
    game = _new_game(client)
    assert [client.post(f"{API}/games/{game['id']}/hint").status_code for _ in range(3)] == [200, 200, 429]
    res = client.post(f"{API}/games/{game['id']}/hint")
    assert "pistas" in res.json()["detail"]
    assert client.get(f"{API}/opponents").json()["limites_diarios"]["pistas"] == 2


def test_hint_when_llm_is_down(settings):
    client = _client(settings, provider=BrokenProvider())
    game = _new_game(client)
    body = client.post(f"{API}/games/{game['id']}/hint").json()
    assert body["fuente"] == "motor"
    assert body["explicacion"].startswith("El motor recomienda a3.")
    assert body["motivos"][0] in body["explicacion"]


def test_hint_without_budget_uses_the_engine_text(settings):
    client = _client(settings)
    client.app.state.limiter.global_cents_limit = 0
    game = _new_game(client)
    body = client.post(f"{API}/games/{game['id']}/hint").json()
    assert body["fuente"] == "motor" and body["explicacion"]


def test_hint_engine_failure_is_503(settings):
    client = _client(settings, engine=FakeEngine(fail=True))
    game = _new_game(client)
    assert client.post(f"{API}/games/{game['id']}/hint").status_code == 503


# ------------------------------------------------------------------ /takeback
def _play(client, game, plies):
    for _ in range(plies):
        game = client.post(f"{API}/games/{game['id']}/move", json={"uci": sorted(game["legal_moves"])[0]}).json()
    return game


def test_takeback_to_an_earlier_position(settings):
    client = _client(settings)
    game = _play(client, _new_game(client), 3)
    assert len(game["moves_uci"]) == 6
    res = client.post(f"{API}/games/{game['id']}/takeback", json={"ply": 2})
    assert res.status_code == 200, res.text
    state = res.json()
    assert state["moves_uci"] == game["moves_uci"][:2] and state["turn"] == "white" and state["legal_moves"]
    assert state["con_ayuda"] is True and state["ayudas"] == {"pistas": 0, "deshacer": 1}
    # Se puede seguir jugando desde ahí.
    state = client.post(f"{API}/games/{game['id']}/move", json={"uci": state["legal_moves"][0]}).json()
    assert len(state["moves_uci"]) == 4
    state = client.post(f"{API}/games/{game['id']}/takeback", json={"ply": 0}).json()
    assert state["moves_uci"] == [] and state["ayudas"]["deshacer"] == 2


def test_takeback_to_a_position_where_the_rival_moves(settings):
    client = _client(settings)
    game = _play(client, _new_game(client), 2)
    res = client.post(f"{API}/games/{game['id']}/takeback", json={"ply": 1})
    assert res.status_code == 422 and "rival" in res.json()["detail"]
    assert client.post(f"{API}/games/{game['id']}/takeback", json={"ply": 99}).status_code == 422
    assert client.post(f"{API}/games/{game['id']}/takeback", json={"ply": -1}).status_code == 422
    assert client.get(f"{API}/games/{game['id']}").json()["con_ayuda"] is False


def test_takeback_as_black(settings):
    client = _client(settings)
    game = _play(client, _new_game(client, color="black"), 2)
    assert client.post(f"{API}/games/{game['id']}/takeback", json={"ply": 0}).status_code == 422
    state = client.post(f"{API}/games/{game['id']}/takeback", json={"ply": 1}).json()
    assert len(state["moves_uci"]) == 1 and state["turn"] == "black"


def test_takeback_same_ply_is_a_no_op(settings):
    client = _client(settings)
    game = _play(client, _new_game(client), 1)
    state = client.post(f"{API}/games/{game['id']}/takeback", json={"ply": 2}).json()
    assert state["moves_uci"] == game["moves_uci"] and state["con_ayuda"] is False


def test_takeback_reopens_a_finished_game_but_its_first_result_stays_in_the_ranking(settings):
    client = _client(settings)
    game = _play(client, _new_game(client), 2)
    client.post(f"{API}/games/{game['id']}/resign")
    rivals = client.get("/api/ranking/meaculpa").json()["rivales"]
    assert rivals and rivals[0]["partidas"] == 1 and rivals[0]["derrotas"] == 1
    state = client.post(f"{API}/games/{game['id']}/takeback", json={"ply": 2}).json()
    assert state["status"] == "en_curso" and state["resultado_usuario"] is None and state["result"] == "*"
    assert state["legal_moves"] and state["con_ayuda"] is True
    # Deshacer no borra la derrota (si no, bastaría con deshacer después de perder para quitársela).
    client.app.state.ranking._cache.clear()
    rivals = client.get("/api/ranking/meaculpa").json()["rivales"]
    assert rivals[0]["partidas"] == 1 and rivals[0]["derrotas"] == 1
    # Y lo que pase después ya no cuenta: sigue siendo una sola partida.
    client.post(f"{API}/games/{game['id']}/resign")
    client.app.state.ranking._cache.clear()
    assert client.get("/api/ranking/meaculpa").json()["rivales"][0]["partidas"] == 1


def test_takeback_of_a_finished_game_at_the_final_position(settings):
    client = _client(settings)
    game = _new_game(client)
    client.post(f"{API}/games/{game['id']}/resign")
    # Abandono en la posición inicial: se puede reabrir ahí mismo (no es final de partida en el tablero).
    state = client.post(f"{API}/games/{game['id']}/takeback", json={"ply": 0}).json()
    assert state["status"] == "en_curso"


def test_game_with_help_is_not_ranked(settings):
    client = _client(settings)
    helped = _new_game(client)
    client.post(f"{API}/games/{helped['id']}/hint")
    client.post(f"{API}/games/{helped['id']}/resign")
    clean = _new_game(client, opponent="greedy")
    client.post(f"{API}/games/{clean['id']}/resign")
    client.app.state.ranking._cache.clear()
    data = client.get("/api/ranking/meaculpa").json()
    assert [r["id"] for r in data["rivales"]] == ["greedy"]
    assert data["jugadores"][0]["partidas"] == 1


def test_hint_quota_is_in_the_admin_panel(settings):
    from test_admin import ADMIN_H
    from test_admin import verifier as admin_verifier

    app = create_app(
        settings=settings, provider=MockProvider(seed=5), token_verifier=admin_verifier, live_engine=FakeEngine()
    )
    client = TestClient(app)
    quotas = {q["tipo"]: q for q in client.get("/api/admin/controles", headers=ADMIN_H).json()["cuotas"]}
    assert quotas["pistas"]["por_defecto"] == 30 and "pistas" in quotas["pistas"]["etiqueta"]
    res = client.post("/api/admin/controles/cuotas", json={"tipo": "pistas", "valor": 1}, headers=ADMIN_H)
    assert res.status_code == 200 and res.json()["valor"] == 1


# ------------------------------------------------------------------ motivos
def test_motives_capture_mate_and_development():
    board = chess.Board("r3k2r/ppp2ppp/2n5/3q4/8/2N5/PPP2PPP/R2QK2R w KQkq - 0 10")
    move = chess.Move.from_uci("c3d5")
    reasons = motives(board, move, [Line(cp=880, mate=None, pv=[move])])
    assert reasons[0] == "Captura la dama de d5 y el rival no puede recapturar: ganas material."
    assert reasons[-1] == "Con ella la valoración queda en +8,8 a tu favor."
    scholar = chess.Board(SCHOLAR)
    mate = chess.Move.from_uci("h5f7")
    assert motives(scholar, mate, [Line(cp=None, mate=1, pv=[mate])]) == ["Da jaque mate: la partida se acaba aquí."]
    start = chess.Board()
    nf3 = chess.Move.from_uci("g1f3")
    assert "Desarrolla el caballo y lo pone en juego." in motives(start, nf3, [Line(cp=30, mate=None, pv=[nf3])])
    fork = chess.Board("r3k3/8/8/8/8/8/2N5/4K3 w - - 0 1")
    nb4 = chess.Move.from_uci("c2b4")
    lines = [Line(cp=50, mate=None, pv=[nb4]), Line(cp=-400, mate=None, pv=[chess.Move.from_uci("e1d2")])]
    reasons = motives(fork, nb4, lines)
    assert any(r.startswith("Es claramente la mejor: la siguiente opción, Kd2") for r in reasons)
    for text in reasons:
        for ch in "·—–":
            assert ch not in text


def test_fallback_explanation_and_clean_text():
    text = fallback_explanation(
        "Nf3", ["Desarrolla el caballo y lo pone en juego.", "Con ella la valoración queda en +0,3 a tu favor."]
    )
    assert (
        text
        == "El motor recomienda Nf3. Desarrolla el caballo y lo pone en juego. Con ella la valoración queda en +0,3 a tu favor."
    )
    assert clean_text("Nf3 — desarrolla · y controla – el centro") == "Nf3, desarrolla, y controla - el centro"


def test_terminal_payload():
    assert terminal_payload(chess.Board()) is None
    assert terminal_payload(chess.Board(FOOLS_MATE))["resultado"] == "0-1"


# ------------------------------------------------------------------ pista y jugada a la vez
class SideEffectEngine(FakeEngine):
    """Como FakeEngine, pero mientras «piensa» otra petición guarda la partida (una pista o una jugada a la vez)."""

    def __init__(self, effect) -> None:
        super().__init__()
        self.effect = effect

    def analyse(self, board, *, time_s=0.2, depth=None, multipv=1):
        if self.effect is not None:
            effect, self.effect = self.effect, None
            effect()
        return super().analyse(board, time_s=time_s, depth=depth, multipv=multipv)


def _hints_used(client, user="ana"):
    limiter = client.app.state.limiter
    found = limiter.objects.read(f"usage/{limiter._day()}/{owner_id(user)}")
    return int(__import__("json").loads(found[0]).get("pistas", 0)) if found else 0


def test_hint_keeps_a_help_mark_saved_meanwhile(settings):
    from meaculpa.games import mark_help

    engine = SideEffectEngine(None)
    client = _client(settings, engine=engine)
    game = _new_game(client)
    store, owner = client.app.state.store, owner_id("ana")

    def other_help():
        record = store.get(owner, game["id"])
        mark_help(record, "pistas")  # otra pista guardada mientras se calculaba esta
        store.save(record)

    engine.effect = other_help
    res = client.post(f"{API}/games/{game['id']}/hint")
    assert res.status_code == 200, res.text
    assert res.json()["ayudas"] == {"pistas": 2, "deshacer": 0}
    assert store.get(owner, game["id"]).ayudas == {"pistas": 2, "deshacer": 0}


def test_hint_is_not_charged_if_you_moved_meanwhile(settings):
    engine = SideEffectEngine(None)
    client = _client(settings, engine=engine)
    game = _new_game(client)
    store, owner = client.app.state.store, owner_id("ana")

    def moved():
        record = store.get(owner, game["id"])
        record.moves = ["e2e4", "e7e5"]  # la jugada (y la respuesta del rival) se guardó antes que la pista
        store.save(record)

    engine.effect = moved
    res = client.post(f"{API}/games/{game['id']}/hint")
    assert res.status_code == 409 and "pista" in res.json()["detail"]
    assert _hints_used(client) == 0
    state = client.get(f"{API}/games/{game['id']}").json()
    assert state["moves_uci"] == ["e2e4", "e7e5"] and state["con_ayuda"] is False


def test_move_survives_a_hint_saved_meanwhile(settings, monkeypatch):
    """La jugada (con un rival lento) no se pierde si mientras tanto se ha guardado una pista."""
    from meaculpa import main as main_module
    from meaculpa.games import mark_help

    client = _client(settings)
    game = _new_game(client)
    store, owner = client.app.state.store, owner_id("ana")
    real = main_module.build_opponent

    def slow_opponent(*args, **kwargs):
        opp = real(*args, **kwargs)
        choose = opp.choose_move

        def choose_move(board):
            record = store.get(owner, game["id"])
            mark_help(record, "pistas")
            store.save(record)
            return choose(board)

        opp.choose_move = choose_move
        return opp

    monkeypatch.setattr(main_module, "build_opponent", slow_opponent)
    res = client.post(f"{API}/games/{game['id']}/move", json={"uci": "e2e4"})
    assert res.status_code == 200, res.text
    body = res.json()
    assert len(body["moves_uci"]) == 2 and body["con_ayuda"] is True and body["ayudas"]["pistas"] == 1


# ------------------------------------------------------------------ diagnóstico de una partida abandonada
def test_resigned_game_tells_the_diagnosis_it_was_a_resignation(settings):
    class Recording(MockProvider):
        def __init__(self):
            super().__init__(seed=5)
            self.seen = []

        def structured(self, messages, schema, temperature=None):
            self.seen.append(messages)
            return super().structured(messages, schema, temperature)

    provider = Recording()
    client = _client(settings, provider=provider)
    game = _play(client, _new_game(client), 3)
    client.post(f"{API}/games/{game['id']}/resign")
    res = client.post(f"{API}/games/{game['id']}/analysis")
    assert res.status_code == 200, res.text
    prompt = provider.seen[-1][1]["content"]
    assert "<fin>abandono</fin>" in prompt and "SE RINDIÓ" in prompt
    assert res.json()["grounding"]["fin"] == "abandono"


def test_motives_in_a_lost_position_do_not_say_it_improves():
    board = chess.Board("4k3/8/8/8/8/8/q7/7K w - - 0 1")
    move = chess.Move.from_uci("h1g1")
    reasons = motives(board, move, [Line(cp=None, mate=-6, pv=[move])], Line(cp=None, mate=-5, pv=[]))
    assert reasons[0].startswith("Es la defensa que más resiste")
    assert not any("Mejora la posición" in r for r in reasons)
