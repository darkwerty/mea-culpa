from __future__ import annotations

import random

from fastapi.testclient import TestClient

from meaculpa.llm import MockProvider
from meaculpa.main import create_app


def _client(settings):
    return TestClient(create_app(settings=settings, provider=MockProvider(seed=5)))


def _play_random(client, state, rng, max_plies=400):
    while state["status"] == "en_curso" and len(state["moves_uci"]) < max_plies:
        assert state["legal_moves"], state
        uci = rng.choice(state["legal_moves"])
        state = client.post(f"/api/games/{state['id']}/move", json={"uci": uci}).json()
    return state


def test_health_and_opponents(settings):
    client = _client(settings)
    assert client.get("/api/health").json()["llm"]["proveedor"] == "mock"
    ids = [o["id"] for o in client.get("/api/opponents").json()["clasicos"]]
    assert ids[:3] == ["random", "greedy", "minimax"]


def test_game_vs_random_then_diagnosis(settings):
    client = _client(settings)
    rng = random.Random(1)
    state = client.post("/api/games", json={"opponent": {"type": "random"}, "user_color": "white"}).json()
    state = _play_random(client, state, rng, max_plies=60)
    if state["status"] == "en_curso":
        state = client.post(f"/api/games/{state['id']}/resign").json()
    assert state["status"] == "terminada"
    assert state["resultado_usuario"] in {"victoria", "derrota", "tablas"}
    res = client.post(f"/api/games/{state['id']}/analysis")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["diagnostico"]["titular"]
    assert len(body["analisis"]["curva_blancas"]) == len(state["moves_uci"]) + 1
    assert client.get("/api/games").json()[0]["id"] == state["id"]


def test_illegal_and_out_of_turn_moves_are_rejected(settings):
    client = _client(settings)
    gid = client.post("/api/games", json={"opponent": {"type": "greedy"}}).json()["id"]
    assert client.post(f"/api/games/{gid}/move", json={"uci": "e2e5"}).status_code == 422
    assert client.post(f"/api/games/{gid}/move", json={"uci": "zzzz"}).status_code == 422
    client.post(f"/api/games/{gid}/resign")
    assert client.post(f"/api/games/{gid}/move", json={"uci": "e2e4"}).status_code == 409


def test_play_black_vs_minimax(settings):
    client = _client(settings)
    payload = {"opponent": {"type": "minimax", "depth": 2}, "user_color": "black"}
    state = client.post("/api/games", json=payload).json()
    assert len(state["moves_uci"]) == 1 and state["turn"] == "black"
    state = client.post(f"/api/games/{state['id']}/move", json={"uci": state["legal_moves"][0]}).json()
    assert len(state["moves_uci"]) == 3


def test_llm_game_records_metrics(settings):
    client = _client(settings)
    rng = random.Random(2)
    state = client.post("/api/games", json={"opponent": {"type": "llm", "temperature": 1.2}}).json()
    state = _play_random(client, state, rng, max_plies=30)
    meta = state["last_opponent_meta"]
    assert meta["engine"] == "llm" and meta["registro"]["attempts"]
    summary = client.get("/api/metrics/llm").json()["resumen"]
    assert summary[0]["modelo"] == "mock-llm" and summary[0]["temperatura"] == 1.2
    assert summary[0]["jugadas"] >= 10


def test_pgn_endpoint_analyses_external_game(settings, pgn_text):
    client = _client(settings)
    res = client.post("/api/analysis/pgn", json={"pgn": pgn_text("mate_del_loco.pgn"), "user_color": "white"})
    assert res.status_code == 200
    assert res.json()["diagnostico"]["errores"][0]["jugada"] == "g4"
    bad = client.post("/api/analysis/pgn", json={"pgn": "1. e4 e4 2. Qxz9", "user_color": "white"})
    assert bad.status_code == 422


def test_stockfish_is_hidden_when_missing(settings, monkeypatch):
    monkeypatch.delenv("STOCKFISH_PATH", raising=False)
    monkeypatch.setenv("PATH", "")
    client = _client(settings)
    assert "stockfish" not in [o["id"] for o in client.get("/api/opponents").json()["clasicos"]]
    assert client.post("/api/games", json={"opponent": {"type": "stockfish"}}).status_code == 400


def test_analysis_can_be_fetched_after_reload(settings):
    client = _client(settings)
    gid = client.post("/api/games", json={"opponent": {"type": "greedy"}}).json()["id"]
    assert client.get(f"/api/games/{gid}/analysis").status_code == 404
    client.post(f"/api/games/{gid}/move", json={"uci": "f2f3"})
    client.post(f"/api/games/{gid}/resign")
    posted = client.post(f"/api/games/{gid}/analysis").json()
    fetched = client.get(f"/api/games/{gid}/analysis").json()
    assert fetched["diagnostico"] == posted["diagnostico"]
    assert client.get(f"/api/games/{gid}").json()["has_diagnosis"]
