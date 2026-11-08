import json

from geo_support import FAKE_PHOTOS
from geo_support import geo_auth as auth

from meaculpa.geo.schemas import GeoGuess
from meaculpa.llm.base import LLMResult

SECRETS = [s for _, pais, region, lat, lon in FAKE_PHOTOS for s in (pais, region, str(lat), str(lon))]
SECRETS += [pid for pid, *_ in FAKE_PHOTOS] + ["Autorsecreto", "Lugarsecreto"]


def _leaks(payload) -> list[str]:
    text = json.dumps(payload, ensure_ascii=False)
    return [s for s in SECRETS if s in text]


class SpyProvider:
    """Proveedor con visión que apunta siempre a (0, 0) y guarda lo que recibe."""

    kind = "spy"

    def __init__(self, name="spy", parsed=True, price_input_per_m=1.0, price_output_per_m=2.0):
        self.name = name
        self.calls: list[tuple] = []
        self.parsed = parsed
        self.price_input_per_m = price_input_per_m
        self.price_output_per_m = price_output_per_m

    def locate(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if not self.parsed:
            return LLMResult(None, "no es json", 1000, 50, 5.0, self.name, "ValidationError: x")
        g = GeoGuess(pais="Nada", region="Golfo", lat=0, lon=0, confianza=0.1, razonamiento_breve="agua")
        return LLMResult(g, g.model_dump_json(), 1000, 50, 5.0, self.name)


def _new(client, user="ana", modelo=None):
    r = client.post("/api/geo/partidas", json={"modelo": modelo}, headers=auth(user))
    assert r.status_code == 200, r.text
    return r.json()


def test_auth_required(make_geo_client):
    c = make_geo_client()
    assert c.post("/api/geo/partidas", json={}).status_code == 401
    assert c.post("/api/geo/partidas", json={}, headers=auth("malo")).status_code == 401
    assert c.get("/api/geo/health").status_code == 200


def test_models_only_vision_and_mock(make_geo_client):
    c = make_geo_client()
    data = c.get("/api/geo/models").json()
    ids = {m["id"] for m in data["modelos"]}
    assert {"gpt-6-luna", "gpt-6-luna-high", "gpt-6.1-terra", "claude-haiku-5-5"} <= ids
    available = [m["id"] for m in data["modelos"] if m["disponible"]]
    assert available == ["mock-geo"] and data["por_defecto"] == "mock-geo"
    assert data["rondas"] == 5


def test_text_only_models_are_filtered(make_geo_client):
    class TextOnly:
        kind = "azure"
        name = "solo-texto"

    c = make_geo_client(models={"solo-texto": TextOnly(), "spy": SpyProvider()})
    ids = [m["id"] for m in c.get("/api/geo/models").json()["modelos"] if m["disponible"]]
    assert ids == ["spy"]


def test_solution_not_leaked_before_answer(make_geo_client):
    c = make_geo_client()
    game = _new(c)
    assert _leaks(game) == []
    assert game["rondas"] == [] and game["ronda_actual"] == 0
    assert game["foto_actual"].endswith("/foto/0")
    assert _leaks(c.get(f"/api/geo/partidas/{game['id']}", headers=auth()).json()) == []
    # La imagen sale sin cabeceras con datos de la foto
    r = c.get(game["foto_actual"], headers=auth())
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    assert _leaks(dict(r.headers)) == []
    # Las fotos de rondas futuras no se pueden pedir
    assert c.get(f"/api/geo/partidas/{game['id']}/foto/1", headers=auth()).status_code == 404
    assert c.get(f"/api/geo/partidas/{game['id']}/foto/-1", headers=auth()).status_code == 404


def test_model_receives_only_the_image(make_geo_client, tmp_path):
    spy = SpyProvider()
    c = make_geo_client(models={"spy": spy})
    game = _new(c)
    r = c.post(f"/api/geo/partidas/{game['id']}/rondas/0", json={"lat": 1, "lon": 1}, headers=auth())
    assert r.status_code == 200
    ((args, kwargs),) = spy.calls
    sent = [a for a in list(args) + list(kwargs.values())]
    image = sent[0]
    files = {p.read_bytes(): p.stem for p in (tmp_path / "fotos").glob("*.jpg")}
    assert image in files  # la imagen tal cual
    assert all(not isinstance(x, (dict, list)) for x in sent[1:])  # nada más que el tipo MIME
    assert _leaks([x for x in sent[1:]]) == []


def test_round_cannot_be_repeated(make_geo_client):
    c = make_geo_client(models={"spy": SpyProvider()})
    game = _new(c)
    gid = game["id"]
    r = c.post(f"/api/geo/partidas/{gid}/rondas/0", json={"lat": 12.3, "lon": 45.6}, headers=auth())
    assert r.status_code == 200
    out = r.json()
    last = out["ultima"]
    assert set(last["solucion"]) == {"pais", "region", "lat", "lon"}
    assert last["atribucion"]["licencia"] == "CC BY-SA 4.0"
    assert last["ia"]["lat"] == 0 and last["ia"]["puntos"] >= 0
    assert out["ronda_actual"] == 1
    # Repetir la misma ronda con otra marca: rechazado y sin cambios
    again = c.post(f"/api/geo/partidas/{gid}/rondas/0", json={"lat": 0, "lon": 0}, headers=auth())
    assert again.status_code == 409
    # Saltarse una ronda: rechazado
    assert c.post(f"/api/geo/partidas/{gid}/rondas/2", json={"lat": 0, "lon": 0}, headers=auth()).status_code == 409
    state = c.get(f"/api/geo/partidas/{gid}", headers=auth()).json()
    assert state["rondas"][0]["jugador"] == last["jugador"]
    # Solo se ha revelado la solución de la ronda jugada
    later = [s for s in _leaks(state) if s not in json.dumps(last, ensure_ascii=False)]
    assert later == []


def test_full_game_and_scoreboard(make_geo_client):
    c = make_geo_client(models={"spy": SpyProvider()})
    game = _new(c)
    for n in range(5):
        r = c.post(f"/api/geo/partidas/{game['id']}/rondas/{n}", json={"lat": 10, "lon": 10}, headers=auth())
        assert r.status_code == 200
    final = r.json()
    assert final["estado"] == "terminada" and final["foto_actual"] is None
    assert len(final["rondas"]) == 5
    assert final["marcador"]["jugador"] == sum(x["jugador"]["puntos"] for x in final["rondas"])
    assert final["marcador"]["ia"] == sum(x["ia"]["puntos"] for x in final["rondas"])
    assert (
        c.post(f"/api/geo/partidas/{game['id']}/rondas/5", json={"lat": 0, "lon": 0}, headers=auth()).status_code == 409
    )


def test_invalid_model_output_scores_zero_and_retries_once(make_geo_client):
    spy = SpyProvider(parsed=False)
    c = make_geo_client(models={"spy": spy})
    game = _new(c)
    r = c.post(f"/api/geo/partidas/{game['id']}/rondas/0", json={"lat": 1, "lon": 1}, headers=auth())
    ia = r.json()["ultima"]["ia"]
    assert ia["puntos"] == 0 and ia["error"] == "respuesta no válida" and ia["distancia_km"] is None
    assert len(spy.calls) == 2


def test_provider_exception_does_not_break_game(make_geo_client):
    class Broken(SpyProvider):
        def locate(self, *a, **k):
            raise RuntimeError("timeout")

    c = make_geo_client(models={"rota": Broken(name="rota")})
    game = _new(c)
    r = c.post(f"/api/geo/partidas/{game['id']}/rondas/0", json={"lat": 1, "lon": 1}, headers=auth())
    assert r.status_code == 200 and r.json()["ultima"]["ia"]["puntos"] == 0


def test_users_are_isolated(make_geo_client):
    c = make_geo_client()
    game = _new(c, "ana")
    assert c.get(f"/api/geo/partidas/{game['id']}", headers=auth("luis")).status_code == 404
    assert c.get(game["foto_actual"], headers=auth("luis")).status_code == 404
    r = c.post(f"/api/geo/partidas/{game['id']}/rondas/0", json={"lat": 1, "lon": 1}, headers=auth("luis"))
    assert r.status_code == 404


def test_quota_llm_per_user(make_geo_client):
    c = make_geo_client(models={"spy": SpyProvider()}, quota_llm_per_day=1)
    game = _new(c)
    assert (
        c.post(f"/api/geo/partidas/{game['id']}/rondas/0", json={"lat": 1, "lon": 1}, headers=auth()).status_code == 200
    )
    r = c.post(f"/api/geo/partidas/{game['id']}/rondas/1", json={"lat": 1, "lon": 1}, headers=auth())
    assert r.status_code == 429
    # Sin cuota la ronda no se consume: sigue siendo la 1
    assert c.get(f"/api/geo/partidas/{game['id']}", headers=auth()).json()["ronda_actual"] == 1
    # Otro usuario tiene su propia cuota
    g2 = _new(c, "luis")
    assert (
        c.post(f"/api/geo/partidas/{g2['id']}/rondas/0", json={"lat": 1, "lon": 1}, headers=auth("luis")).status_code
        == 200
    )


def test_quota_games_and_global_fuse(make_geo_client):
    c = make_geo_client(models={"spy": SpyProvider()}, quota_games_per_day=1, global_llm_per_day=1)
    g = _new(c, "ana")
    assert c.post("/api/geo/partidas", json={}, headers=auth("ana")).status_code == 429
    assert c.post(f"/api/geo/partidas/{g['id']}/rondas/0", json={"lat": 1, "lon": 1}, headers=auth()).status_code == 200
    g2 = _new(c, "luis")
    r = c.post(f"/api/geo/partidas/{g2['id']}/rondas/0", json={"lat": 1, "lon": 1}, headers=auth("luis"))
    assert r.status_code == 503


def test_cost_recorded(make_geo_client):
    c = make_geo_client(models={"spy": SpyProvider()})
    game = _new(c)
    out = c.post(f"/api/geo/partidas/{game['id']}/rondas/0", json={"lat": 1, "lon": 1}, headers=auth()).json()
    expected = (1000 * 1.0 + 50 * 2.0) / 1_000_000
    assert out["ultima"]["coste_usd"] == round(expected, 6)
    assert out["ultima"]["ia"]["tokens_entrada"] == 1000
    costs = c.get("/api/geo/costes", headers=auth()).json()["hoy"]["spy"]
    assert costs["llamadas"] == 1 and costs["usd"] == round(expected, 6)


def test_new_games_prefer_unseen_photos(make_geo_client):
    c = make_geo_client(models={"spy": SpyProvider()})
    a = _new(c)
    for n in range(5):
        c.post(f"/api/geo/partidas/{a['id']}/rondas/{n}", json={"lat": 1, "lon": 1}, headers=auth())
    seen = {c.get(f"/api/geo/partidas/{a['id']}/foto/{n}", headers=auth()).content for n in range(5)}
    assert len(seen) == 5
    b = _new(c)
    # 6 fotos y 5 por partida: la segunda empieza por la única que no se ha visto
    assert c.get(f"/api/geo/partidas/{b['id']}/foto/0", headers=auth()).content not in seen


def test_unknown_model_rejected(make_geo_client):
    c = make_geo_client()
    assert c.post("/api/geo/partidas", json={"modelo": "gpt-6-luna"}, headers=auth()).status_code == 400
