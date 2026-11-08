from __future__ import annotations

import dataclasses
import random

import pytest
from conftest import auth

from meaculpa.analysis import AnalysisTimeout
from meaculpa.auth import owner_id
from meaculpa.games import GameConflictError


def _play_random(client, state, rng, max_plies=400):
    while state["status"] == "en_curso" and len(state["moves_uci"]) < max_plies:
        assert state["legal_moves"], state
        uci = rng.choice(state["legal_moves"])
        state = client.post(f"/api/meaculpa/games/{state['id']}/move", json={"uci": uci}).json()
    return state


def test_health_and_opponents(settings, make_client):
    client = make_client(settings)
    assert client.get("/api/meaculpa/health").json() == {"ok": True}
    body = client.get("/api/meaculpa/opponents").json()
    assert body["llm"]["proveedor"] == "mock" and body["usuario"] == "Ana"
    ids = [o["id"] for o in body["clasicos"]]
    assert ids[:3] == ["random", "greedy", "minimax"]


def test_game_vs_random_then_diagnosis(settings, make_client):
    client = make_client(settings)
    rng = random.Random(1)
    state = client.post("/api/meaculpa/games", json={"opponent": {"type": "random"}, "user_color": "white"}).json()
    state = _play_random(client, state, rng, max_plies=60)
    if state["status"] == "en_curso":
        state = client.post(f"/api/meaculpa/games/{state['id']}/resign").json()
    assert state["status"] == "terminada"
    assert state["resultado_usuario"] in {"victoria", "derrota", "tablas"}
    res = client.post(f"/api/meaculpa/games/{state['id']}/analysis")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["diagnostico"]["titular"]
    assert len(body["analisis"]["curva_blancas"]) == len(state["moves_uci"]) + 1
    assert client.get("/api/meaculpa/games").json()[0]["id"] == state["id"]


def test_illegal_and_out_of_turn_moves_are_rejected(settings, make_client):
    client = make_client(settings)
    gid = client.post("/api/meaculpa/games", json={"opponent": {"type": "greedy"}}).json()["id"]
    assert client.post(f"/api/meaculpa/games/{gid}/move", json={"uci": "e2e5"}).status_code == 422
    assert client.post(f"/api/meaculpa/games/{gid}/move", json={"uci": "zzzz"}).status_code == 422
    client.post(f"/api/meaculpa/games/{gid}/resign")
    assert client.post(f"/api/meaculpa/games/{gid}/move", json={"uci": "e2e4"}).status_code == 409


def test_play_black_vs_minimax(settings, make_client):
    client = make_client(settings)
    payload = {"opponent": {"type": "minimax", "depth": 2}, "user_color": "black"}
    state = client.post("/api/meaculpa/games", json=payload).json()
    assert len(state["moves_uci"]) == 1 and state["turn"] == "black"
    state = client.post(f"/api/meaculpa/games/{state['id']}/move", json={"uci": state["legal_moves"][0]}).json()
    assert len(state["moves_uci"]) == 3


def test_llm_game_records_metrics(settings, make_client):
    client = make_client(settings)
    rng = random.Random(2)
    state = client.post("/api/meaculpa/games", json={"opponent": {"type": "llm", "temperature": 1.2}}).json()
    state = _play_random(client, state, rng, max_plies=30)
    meta = state["last_opponent_meta"]
    assert meta["engine"] == "llm" and meta["registro"]["attempts"]
    summary = client.get("/api/meaculpa/metrics/llm").json()["resumen"]
    assert summary[0]["modelo"] == "mock-llm" and summary[0]["temperatura"] == 1.2
    assert summary[0]["jugadas"] >= 10


def test_pgn_endpoint_analyses_external_game(settings, pgn_text, make_client):
    client = make_client(settings)
    res = client.post("/api/meaculpa/analysis/pgn", json={"pgn": pgn_text("mate_del_loco.pgn"), "user_color": "white"})
    assert res.status_code == 200
    assert res.json()["diagnostico"]["errores"][0]["jugada"] == "g4"
    bad = client.post("/api/meaculpa/analysis/pgn", json={"pgn": "1. e4 e4 2. Qxz9", "user_color": "white"})
    assert bad.status_code == 422


def test_stockfish_is_hidden_when_missing(settings, monkeypatch, make_client):
    monkeypatch.delenv("STOCKFISH_PATH", raising=False)
    monkeypatch.setenv("PATH", "")
    client = make_client(settings)
    assert "stockfish" not in [o["id"] for o in client.get("/api/meaculpa/opponents").json()["clasicos"]]
    assert client.post("/api/meaculpa/games", json={"opponent": {"type": "stockfish"}}).status_code == 400


def test_analysis_can_be_fetched_after_reload(settings, make_client):
    client = make_client(settings)
    gid = client.post("/api/meaculpa/games", json={"opponent": {"type": "greedy"}}).json()["id"]
    assert client.get(f"/api/meaculpa/games/{gid}/analysis").status_code == 404
    client.post(f"/api/meaculpa/games/{gid}/move", json={"uci": "f2f3"})
    client.post(f"/api/meaculpa/games/{gid}/resign")
    posted = client.post(f"/api/meaculpa/games/{gid}/analysis").json()
    fetched = client.get(f"/api/meaculpa/games/{gid}/analysis").json()
    assert fetched["diagnostico"] == posted["diagnostico"]
    assert client.get(f"/api/meaculpa/games/{gid}").json()["has_diagnosis"]


def test_pgn_analysis_rejects_too_many_plies(settings, make_client):
    client = make_client(dataclasses.replace(settings, max_analysis_plies=10))
    moves = "1. Nf3 Nf6 2. Ng1 Ng8 3. Nf3 Nf6 4. Ng1 Ng8 5. Nc3 Nc6 6. Nb1 Nb8 *"
    res = client.post("/api/meaculpa/analysis/pgn", json={"pgn": moves, "user_color": "white"})
    assert res.status_code == 422 and "demasiado larga" in res.json()["detail"]
    assert (
        client.post("/api/meaculpa/analysis/pgn", json={"pgn": "1. e4 " * 4000, "user_color": "white"}).status_code
        == 422
    )


def test_analysis_timeout_returns_503(settings, make_client, monkeypatch, pgn_text):
    import meaculpa.main as main_mod

    def too_slow(*args, **kwargs):
        raise AnalysisTimeout()

    monkeypatch.setattr(main_mod, "analyze_moves", too_slow)
    client = make_client(settings)
    res = client.post("/api/meaculpa/analysis/pgn", json={"pgn": pgn_text("mate_del_loco.pgn"), "user_color": "white"})
    assert res.status_code == 503


def test_daily_quotas_are_enforced(settings, make_client):
    client = make_client(dataclasses.replace(settings, quota_games_per_day=2, quota_analyses_per_day=1))
    for _ in range(2):
        assert client.post("/api/meaculpa/games", json={"opponent": {"type": "random"}}).status_code == 200
    res = client.post("/api/meaculpa/games", json={"opponent": {"type": "random"}})
    assert res.status_code == 429 and "límite diario" in res.json()["detail"]
    # Otro usuario tiene su propia cuota.
    assert (
        client.post("/api/meaculpa/games", json={"opponent": {"type": "random"}}, headers=auth("bea")).status_code
        == 200
    )
    pgn = "1. f3 e5 2. g4 Qh4# 0-1"
    assert client.post("/api/meaculpa/analysis/pgn", json={"pgn": pgn, "user_color": "white"}).status_code == 200
    assert client.post("/api/meaculpa/analysis/pgn", json={"pgn": pgn, "user_color": "white"}).status_code == 429


def test_quota_survives_a_new_replica(settings, make_client):
    """Los contadores están en el almacén compartido, no en memoria: otra instancia de la API los ve."""
    limited = dataclasses.replace(settings, quota_games_per_day=1)
    assert make_client(limited).post("/api/meaculpa/games", json={"opponent": {"type": "random"}}).status_code == 200
    assert make_client(limited).post("/api/meaculpa/games", json={"opponent": {"type": "random"}}).status_code == 429


def test_global_llm_fuse(settings, make_client):
    # Cada jugada del LLM reserva 1 + llm_max_retries llamadas; con 5 solo cabe una jugada.
    client = make_client(dataclasses.replace(settings, global_llm_calls_per_day=5, llm_max_retries=3))
    state = client.post("/api/meaculpa/games", json={"opponent": {"type": "llm"}}).json()
    state = client.post(f"/api/meaculpa/games/{state['id']}/move", json={"uci": "e2e4"}).json()
    assert len(state["moves_uci"]) == 2
    res = client.post(
        f"/api/meaculpa/games/{state['id']}/move", json={"uci": state["legal_moves"][0]}, headers=auth("ana")
    )
    assert res.status_code == 503 and "presupuesto diario" in res.json()["detail"]


def test_games_are_isolated_between_users(settings, make_client):
    ana = make_client(settings, "ana")
    bea = make_client(settings, "bea")
    gid = ana.post("/api/meaculpa/games", json={"opponent": {"type": "greedy"}}).json()["id"]
    ana.post(f"/api/meaculpa/games/{gid}/move", json={"uci": "f2f3"})

    assert [g["id"] for g in ana.get("/api/meaculpa/games").json()] == [gid]
    assert bea.get("/api/meaculpa/games").json() == []
    assert bea.get(f"/api/meaculpa/games/{gid}").status_code == 404
    assert bea.post(f"/api/meaculpa/games/{gid}/move", json={"uci": "g2g4"}).status_code == 404
    assert bea.post(f"/api/meaculpa/games/{gid}/resign").status_code == 404
    assert bea.post(f"/api/meaculpa/games/{gid}/analysis").status_code == 404
    assert bea.get(f"/api/meaculpa/games/{gid}/analysis").status_code == 404
    assert ana.get(f"/api/meaculpa/games/{gid}").json()["status"] == "en_curso"


def test_pattern_history_is_per_user(settings, make_client):
    ana = make_client(settings, "ana")
    bea = make_client(settings, "bea")
    for client in (ana, ana, bea):
        gid = client.post("/api/meaculpa/games", json={"opponent": {"type": "greedy"}}).json()["id"]
        client.post(f"/api/meaculpa/games/{gid}/move", json={"uci": "f2f3"})
        client.post(f"/api/meaculpa/games/{gid}/resign")
        last = client.post(f"/api/meaculpa/games/{gid}/analysis").json()
    assert last["analisis"]["historial"]["partidas"] == 1  # Bea no ve las dos partidas de Ana
    assert ana.get(f"/api/meaculpa/games/{gid}").status_code == 404


def test_stored_game_has_no_email_or_raw_sub(settings, make_client):
    client = make_client(settings, "1234567890")
    gid = client.post("/api/meaculpa/games", json={"opponent": {"type": "random"}}).json()["id"]
    files = list((settings.data_dir / "store" / "games").rglob(f"{gid}.json"))
    assert len(files) == 1
    assert "1234567890" not in files[0].as_posix() and "1234567890" not in files[0].read_text(encoding="utf-8")


def test_concurrent_writes_to_a_game_conflict(settings, make_client):
    client = make_client(settings)
    gid = client.post("/api/meaculpa/games", json={"opponent": {"type": "random"}}).json()["id"]
    store = client.app.state.store
    owner = owner_id("ana")
    first, second = store.get(owner, gid), store.get(owner, gid)
    first.moves.append("e2e4")
    store.save(first)
    second.moves.append("d2d4")
    with pytest.raises(GameConflictError):
        store.save(second)


def test_models_catalog_and_choice(settings):
    from conftest import fake_verifier
    from fastapi.testclient import TestClient

    from meaculpa.llm import MockProvider
    from meaculpa.main import create_app

    models = {"gpt-4.1-mini": MockProvider(seed=1), "claude-haiku-5-5": MockProvider(seed=2)}
    app = create_app(settings=settings, provider=MockProvider(seed=5), token_verifier=fake_verifier, models=models)
    anon = TestClient(app)
    body = anon.get("/api/meaculpa/models").json()  # público: el selector se pinta antes del login
    by_id = {m["id"]: m for m in body["modelos"]}
    assert set(by_id) >= {"gpt-4.1-mini", "gpt-6-luna", "gpt-6-luna-high", "gpt-6.1-terra", "claude-haiku-5-5"}
    assert by_id["claude-haiku-5-5"]["disponible"] and not by_id["gpt-6.1-terra"]["disponible"]
    assert body["por_defecto"] == "gpt-4.1-mini"

    client = TestClient(app, headers=auth("ana"))
    state = client.post(
        "/api/meaculpa/games", json={"opponent": {"type": "llm", "model": "claude-haiku-5-5"}, "user_color": "black"}
    ).json()
    assert state["opponent"]["model"] == "claude-haiku-5-5"
    assert state["opponent"]["modelo_etiqueta"].startswith("Claude Haiku 5.5")
    assert state["last_opponent_meta"]["engine"] == "llm"
    no = client.post("/api/meaculpa/games", json={"opponent": {"type": "llm", "model": "gpt-6.1-terra"}})
    assert no.status_code == 400
    plain = client.post("/api/meaculpa/games", json={"opponent": {"type": "random", "model": "gpt-6.1-terra"}}).json()
    assert "model" not in plain["opponent"]
