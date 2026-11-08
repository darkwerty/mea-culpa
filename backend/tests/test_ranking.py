"""Rankings: el resultado lo calcula el servidor, la casilla oculta el nombre y la lectura es pública."""

import dataclasses

from conftest import auth as chess_auth
from geo_support import geo_auth

from meaculpa.geo.vision import parse_json_guess
from meaculpa.llm.foundry import inference_endpoint, strip_reasoning
from meaculpa.models import CATALOG_BY_ID, active_model_ids, rival_label
from meaculpa.ranking import chess_rival, clean_name


def _play_geo(client, user, ranking=True):
    r = client.post("/api/geo/partidas", json={"modelo": None, "ranking": ranking}, headers=geo_auth(user))
    assert r.status_code == 200, r.text
    game = r.json()
    for n in range(game["total_rondas"]):
        r = client.post(
            f"/api/geo/partidas/{game['id']}/rondas/{n}", json={"lat": 10, "lon": 10}, headers=geo_auth(user)
        )
        assert r.status_code == 200, r.text
    return r.json()


def test_geo_ranking_public_and_server_scored(make_geo_client):
    c = make_geo_client()
    empty = c.get("/api/ranking/geo")
    assert empty.status_code == 200 and empty.json()["jugadores"] == []
    final = _play_geo(c, "ana")
    _play_geo(c, "luis", ranking=False)
    body = c.get("/api/ranking/geo")
    assert body.headers["cache-control"].startswith("public")
    data = body.json()
    names = [p["nombre"] for p in data["jugadores"]]
    assert names == ["ana"]  # luis desmarcó la casilla: no sale con su nombre
    assert data["jugadores"][0]["puntos"] == final["marcador"]["jugador"]
    model = data["modelos"][0]
    assert model["partidas"] == 2  # las dos cuentan, sin nombre, en la estadística del modelo
    assert model["gana_humano"] + model["gana_ia"] + model["empates"] == 2
    assert "owner" not in body.text and "@" not in body.text


def test_geo_unfinished_game_not_ranked(make_geo_client):
    c = make_geo_client()
    game = c.post("/api/geo/partidas", json={}, headers=geo_auth("eva")).json()
    c.post(f"/api/geo/partidas/{game['id']}/rondas/0", json={"lat": 1, "lon": 1}, headers=geo_auth("eva"))
    assert c.get("/api/ranking/geo").json()["jugadores"] == []


def test_preference_hides_and_shows_name(make_geo_client):
    c = make_geo_client()
    _play_geo(c, "ana")
    assert c.post("/api/ranking/preferencia", json={"publico": False}, headers=geo_auth("ana")).status_code == 200
    app = c.app
    app.state.ranking._cache.clear()
    assert c.get("/api/ranking/geo").json()["jugadores"] == []
    c.post("/api/ranking/preferencia", json={"publico": True}, headers=geo_auth("ana"))
    app.state.ranking._cache.clear()
    assert [p["nombre"] for p in c.get("/api/ranking/geo").json()["jugadores"]] == ["ana"]
    assert c.post("/api/ranking/preferencia", json={"publico": False}).status_code == 401
    assert c.get("/api/ranking/otra").status_code == 404


def test_chess_resign_and_rivals(make_client, settings):
    client = make_client(settings, user="ana")
    game = client.post("/api/meaculpa/games", json={"opponent": {"type": "random"}}).json()
    client.post(f"/api/meaculpa/games/{game['id']}/resign")
    client.post(f"/api/meaculpa/games/{game['id']}/resign")  # dos veces: cuenta una
    game2 = client.post(
        "/api/meaculpa/games", json={"opponent": {"type": "minimax", "depth": 2}, "ranking": False}
    ).json()
    client.post(f"/api/meaculpa/games/{game2['id']}/resign")
    data = client.get("/api/ranking/meaculpa").json()
    # La última casilla (desmarcada) vale para todas sus partidas: no sale en la tabla de jugadores.
    assert data["jugadores"] == []
    rivals = {r["id"]: r for r in data["rivales"]}
    assert rivals["random"]["derrotas"] == 1 and rivals["random"]["partidas"] == 1
    assert rivals["minimax-2"]["rival"] == "Minimax, profundidad 2"
    client.post("/api/ranking/preferencia", json={"publico": True}, headers=chess_auth("ana"))
    client.app.state.ranking._cache.clear()
    players = client.get("/api/ranking/meaculpa").json()["jugadores"]
    assert players == [{"nombre": "Ana", "partidas": 2, "victorias": 0, "tablas": 0, "derrotas": 2}]


def test_daily_budget_fuse(make_geo_client):
    c = make_geo_client()
    limiter = c.app.state.limiter
    limiter.global_cents_limit = 1
    limiter.add_cost(0.02)
    game = c.post("/api/geo/partidas", json={}, headers=geo_auth("ana")).json()
    r = c.post(f"/api/geo/partidas/{game['id']}/rondas/0", json={"lat": 1, "lon": 1}, headers=geo_auth("ana"))
    assert r.status_code == 503 and "presupuesto" in r.json()["detail"]


def test_catalog_helpers(settings):
    s = dataclasses.replace(settings, llm_models=("todos", "gpt-6-luna"))
    ids = active_model_ids(s)
    assert "gpt-6-luna" in ids and "gpt-6.1-terra" not in ids and "llama-4-maverick" in ids
    assert rival_label("grok-4.1-fast-reasoning") == "Grok 4.1 Fast razonando"
    assert rival_label("grok-4.1-fast") == "Grok 4.1 Fast"
    assert chess_rival({"type": "stockfish", "skill": 5}) == ("stockfish-5", "Stockfish, nivel 5")
    assert chess_rival({"type": "llm", "model": "kimi-k2.5"}) == ("kimi-k2.5", "Kimi K2.5")
    assert all(CATALOG_BY_ID[i].proveedor in {"azure", "foundry", "claude"} for i in CATALOG_BY_ID)
    assert clean_name("Ana · <b>María</b> — López") == "Ana bMaría/b López"
    assert clean_name("") is None


def test_foundry_helpers():
    assert inference_endpoint("https://acct-1.openai.azure.com/") == "https://acct-1.services.ai.azure.com"
    assert inference_endpoint(None, "https://x.services.ai.azure.com/models/") == "https://x.services.ai.azure.com"
    assert strip_reasoning("<think>hmm Jugada: e4</think>\nJugada: d4") == "Jugada: d4"
    guess, err = parse_json_guess(
        '<think>pienso</think>```json\n{"pais": "Japón", "region": "Nagano", "lat": 36.6, "lon": 138.2, '
        '"confianza": 70, "razonamiento_breve": "Montañas", "extra": 1}\n```'
    )
    assert err is None and guess.pais == "Japón" and guess.confianza == 0.7
    assert parse_json_guess("no sé")[0] is None
