"""¿Dónde está? con Street View: reserva de pano_id, metadatos falsos, cupos, respaldo con fotos y reanudar."""

import json
import logging
import random
import time

import pytest
from geo_support import SV_KEY, FakeGoogle
from geo_support import geo_auth as auth
from test_geo_api import SpyProvider

from meaculpa.geo.config import GeoSettings
from meaculpa.geo.store import GeoGame, GeoStore
from meaculpa.geo.streetview import (
    POOL_KEY,
    Countries,
    PanoPool,
    StreetViewClient,
    is_official,
    pacific_day,
)
from meaculpa.storage import FileObjectStore

SV = {"sv_pool_background": False, "sv_pool_target": 12, "sv_pool_low": 6}


@pytest.fixture
def sv_client(make_geo_client):
    """Cliente con Street View (Google falso) y un modelo espía que apunta a (0, 0)."""

    def _make(google=None, spy=None, **overrides):
        google = google or FakeGoogle()
        spy = spy or SpyProvider()
        c = make_geo_client(models={"spy": spy}, sv_key=SV_KEY, http_get=google, **{**SV, **overrides})
        c.google, c.spy = google, spy
        return c

    return _make


def _new(c, user="ana", **body):
    r = c.post("/api/geo/partidas", json={"modelo": None, **body}, headers=auth(user))
    assert r.status_code == 200, r.text
    return r.json()


def _answer(c, game, n, user="ana", lat=10.0, lon=10.0):
    return c.post(f"/api/geo/partidas/{game['id']}/rondas/{n}", json={"lat": lat, "lon": lon}, headers=auth(user))


def _secrets_of(google: FakeGoogle) -> list[str]:
    """Coordenadas de las panorámicas reales (en varios redondeos) que no pueden salir antes de responder."""
    out = []
    for lat, lon in google.panos.values():
        out += [f"{lat:.4f}", f"{lon:.4f}"]
    return out


# ------------------------------------------------------------------ piezas sueltas
def test_pacific_day_follows_google_reset():
    import calendar

    def ts(s):
        return calendar.timegm(time.strptime(s, "%Y-%m-%d %H:%M"))

    assert pacific_day(ts("2026-11-08 07:59")) == "2026-11-07"  # medianoche PST = 8:00 UTC
    assert pacific_day(ts("2026-11-08 08:00")) == "2026-11-08"
    assert pacific_day(ts("2026-07-01 06:59")) == "2026-06-30"  # medianoche PDT = 7:00 UTC
    assert pacific_day(ts("2026-07-01 07:00")) == "2026-07-01"


def test_country_sampling_stays_on_land_and_spread():
    countries = Countries()
    rng = random.Random(7)
    seen = set()
    for _ in range(300):
        c = countries.pick(rng)
        lat, lon = c.random_point(rng)
        assert c.contains(lat, lon)  # dentro del contorno: nada de puntos en el mar
        seen.add(c.continent)
    assert seen == set(countries.continents)
    assert countries.belongs("ES", 40.42, -3.70) and not countries.belongs("ES", 43.3, -1.0)  # Biarritz es Francia


def test_only_official_panoramas():
    assert is_official({"status": "OK", "copyright": "© 2026 Google", "pano_id": "a" * 22, "location": {}})
    assert not is_official({"status": "OK", "copyright": "© Persona", "pano_id": "a" * 22, "location": {}})
    assert not is_official({"status": "OK", "copyright": "© Google", "pano_id": "CAoS" + "a" * 32, "location": {}})
    assert not is_official({"status": "ZERO_RESULTS"})


def test_pool_keeps_only_official_pano_ids(tmp_path):
    google = FakeGoogle(user_every=3)
    objects = FileObjectStore(tmp_path)
    geo = GeoSettings(photos_dir=tmp_path, sv_key=SV_KEY, http_get=google, **SV)
    pool = PanoPool(objects, StreetViewClient(SV_KEY, google), geo, rng=random.Random(3))
    assert pool.refill() == 12
    stored = json.loads(objects.read(POOL_KEY)[0])["panos"]
    assert len(stored) == 12 and len({e["pano"] for e in stored}) == 12
    assert all(set(e) == {"pano", "pais", "ts"} for e in stored)  # solo el pano_id de Google, nada de coordenadas
    assert all(e["pano"].startswith("P") for e in stored)  # ninguna de usuarios
    raw = objects.read(POOL_KEY)[0].decode()
    assert not any(s in raw for s in _secrets_of(google))
    # Se reparte cada una una sola vez
    a, b = pool.take(5), pool.take(5)
    assert not {e["pano"] for e in a} & {e["pano"] for e in b} and pool.size() == 2
    assert pool.take(5) == []


# ------------------------------------------------------------------ partida con Street View
def test_streetview_game_reveals_only_pano_id(sv_client, tmp_path):
    c = sv_client()
    game = _new(c)
    assert game["fuente"] == "streetview" and game["aviso"] is None
    assert set(game["ubicacion_actual"]) == {"pano", "heading"}
    assert game["foto_actual"] is None and game["rondas"] == []
    text = json.dumps(game)
    assert not [s for s in _secrets_of(c.google) if s in text]
    countries = Countries()
    assert not any(c_.name in text for c_ in countries.countries.values())
    # Con Street View no hay fotos que pedir
    assert c.get(f"/api/geo/partidas/{game['id']}/foto/0", headers=auth()).status_code == 404
    # La partida guardada solo lleva pano_id (y el código de país propio), nunca coordenadas de Google
    stored = (tmp_path / "store" / "geo").rglob("*")
    blob = "".join(p.read_text(encoding="utf-8", errors="ignore") for p in stored if p.is_file())
    assert game["ubicacion_actual"]["pano"] in blob
    assert not [s for s in _secrets_of(c.google) if s in blob]


def test_streetview_round_sends_captures_and_scores_on_server(sv_client, tmp_path):
    c = sv_client()
    game = _new(c)
    pano = game["ubicacion_actual"]["pano"]
    lat, lon = c.google.panos[pano]
    before = c.google.static
    r = _answer(c, game, 0, lat=lat + 0.01, lon=lon)  # a poco más de 1 km
    assert r.status_code == 200, r.text
    out = r.json()
    last = out["ultima"]
    # 4 capturas de esa misma panorámica, en memoria, a la IA
    assert c.google.static - before == 4
    ((args, _),) = c.spy.calls
    images = args[0]
    assert isinstance(images, list) and len(images) == 4
    assert all(pano.encode() in img for img in images)
    headings = sorted(float(p["heading"]) for u, p in c.google.calls if u.endswith("/streetview"))
    assert [round(b - a) for a, b in zip(headings, headings[1:], strict=False)] == [90, 90, 90]
    # Puntos del servidor y solución pedida en el momento
    assert last["solucion"]["lat"] == pytest.approx(lat) and last["solucion"]["lon"] == pytest.approx(lon)
    assert last["jugador"]["puntos"] > 4990 and last["pano"] == pano and "atribucion" not in last
    assert out["ronda_actual"] == 1 and out["ubicacion_actual"]["pano"] != pano
    # Ni las capturas ni las coordenadas se guardan
    blob = b"".join(p.read_bytes() for p in (tmp_path / "store").rglob("*") if p.is_file())
    assert b"captura-" not in blob
    assert f"{lat:.4f}".encode() not in blob


def test_round_answered_twice_is_rejected(sv_client):
    c = sv_client()
    game = _new(c)
    assert _answer(c, game, 0).status_code == 200
    again = _answer(c, game, 0, lat=0, lon=0)
    assert again.status_code == 409
    assert _answer(c, game, 2).status_code == 409  # saltarse una ronda tampoco
    state = c.get(f"/api/geo/partidas/{game['id']}", headers=auth()).json()
    assert len(state["rondas"]) == 1 and state["rondas"][0]["jugador"]["lat"] == 10.0
    assert len(c.spy.calls) == 1


def test_full_streetview_game_goes_to_its_own_ranking(sv_client):
    c = sv_client()
    game = _new(c)
    for n in range(5):
        r = _answer(c, game, n)
        assert r.status_code == 200
    final = r.json()
    assert final["estado"] == "terminada" and final["ubicacion_actual"] is None
    assert all(x["solucion"]["lat"] is not None for x in final["rondas"])
    sv_rank = c.get("/api/ranking/geo-sv").json()
    photo_rank = c.get("/api/ranking/geo").json()
    assert [p["puntos"] for p in sv_rank["jugadores"]] == [final["marcador"]["jugador"]]
    assert photo_rank["jugadores"] == []  # las de fotos van aparte
    # Terminada: ya no hay partida que retomar
    assert c.get("/api/geo/partidas/actual", headers=auth()).json() == {"partida": None}


# ------------------------------------------------------------------ reanudar
def test_reload_resumes_same_round(sv_client):
    c = sv_client()
    game = _new(c)
    assert _answer(c, game, 0).status_code == 200
    state = c.get(f"/api/geo/partidas/{game['id']}", headers=auth()).json()
    current = c.get("/api/geo/partidas/actual", headers=auth()).json()["partida"]
    assert current["id"] == game["id"] and current["ronda_actual"] == 1
    assert current["ubicacion_actual"] == state["ubicacion_actual"]
    assert len(current["rondas"]) == 1
    # Volver a «Empezar partida» no reparte otra: devuelve la misma en la misma ronda
    again = _new(c)
    assert again["id"] == game["id"] and again["reanudada"] is True
    assert again["ubicacion_actual"] == state["ubicacion_actual"]
    # Otro usuario no la ve
    assert c.get("/api/geo/partidas/actual", headers=auth("luis")).json() == {"partida": None}
    # Abandonar empieza otra (y la abandonada no cuenta)
    fresh = _new(c, abandonar=True)
    assert fresh["id"] != game["id"] and fresh["ronda_actual"] == 0
    old = c.get(f"/api/geo/partidas/{game['id']}", headers=auth()).json()
    assert old["estado"] == "abandonada" and old["ubicacion_actual"] is None
    assert _answer(c, old, 1).status_code == 409


def test_pending_round_hidden_then_closed_if_stale(sv_client, tmp_path):
    c = sv_client()
    game = _new(c)
    store = GeoStore(FileObjectStore(tmp_path / "store"))
    owner = next(p.name for p in (tmp_path / "store" / "geo").iterdir() if p.is_dir() and p.name != "sv")
    g = store.get(owner, game["id"])
    g.rondas.append(
        {"jugador": {"lat": 1, "lon": 1, "distancia_km": 5.0, "puntos": 4990}, "ia": None, "t": time.time()}
    )
    store.save(g)
    pending = c.get("/api/geo/partidas/actual", headers=auth()).json()["partida"]
    assert pending["pendiente"] is True and pending["rondas"] == [] and pending["ubicacion_actual"] is None
    assert _answer(c, pending, 1).status_code == 409  # mientras la IA piensa no se puede seguir
    g = store.get(owner, game["id"])
    g.rondas[-1]["t"] = time.time() - 600  # la réplica se apagó a media ronda
    store.save(g)
    closed = c.get("/api/geo/partidas/actual", headers=auth()).json()["partida"]
    assert closed["pendiente"] is False and closed["rondas"][0]["ia"]["puntos"] == 0
    assert closed["ronda_actual"] == 1 and closed["ubicacion_actual"]["pano"]


# ------------------------------------------------------------------ cupos y respaldo con fotos
def test_without_key_games_use_photos(make_geo_client):
    c = make_geo_client(models={"spy": SpyProvider()})
    game = _new(c)
    assert game["fuente"] == "fotos" and "Wikimedia Commons" in game["aviso"]
    assert game["foto_actual"] and game["ubicacion_actual"] is None
    assert c.get("/api/geo/models").json()["streetview"] is False


def test_daily_streetview_cap_falls_back_to_photos(sv_client):
    c = sv_client(sv_static_per_day=20)  # 5 rondas x 4 capturas: cabe una partida al día
    first = _new(c, "ana")
    assert first["fuente"] == "streetview"
    second = _new(c, "luis")
    assert second["fuente"] == "fotos"
    assert second["aviso"].startswith("Cupo de Street View de hoy agotado")
    costes = c.get("/api/geo/costes", headers=auth()).json()
    assert costes["google_hoy"]["sv_partidas"] == 1 and costes["google_limites"]["sv_partidas"] == 1
    # La partida de fotos se juega igual, con la foto y su atribución
    r = _answer(c, second, 0, "luis")
    assert r.status_code == 200 and r.json()["ultima"]["atribucion"]["licencia"]


def test_static_capture_cap_never_exceeded(sv_client):
    c = sv_client(sv_static_per_day=500)  # se recorta al techo duro, por debajo de los 300 de Google
    costes = c.get("/api/geo/costes", headers=auth()).json()
    assert costes["google_limites"]["sv_imagenes"] == 290
    assert costes["google_limites"]["sv_partidas"] == 290 // 20


def test_per_person_streetview_quota(sv_client):
    c = sv_client(quota_sv_games_per_day=1)
    assert _new(c)["fuente"] == "streetview"
    second = _new(c, abandonar=True)
    assert second["fuente"] == "fotos" and second["aviso"].startswith("Ya has jugado hoy tu")
    assert _new(c, "luis")["fuente"] == "streetview"  # cada persona tiene la suya


def test_google_down_falls_back_and_refunds(sv_client):
    google = FakeGoogle(fail=True)
    c = sv_client(google=google)
    game = _new(c)
    assert game["fuente"] == "fotos" and "no responde" in game["aviso"]
    costes = c.get("/api/geo/costes", headers=auth()).json()
    assert costes["google_hoy"]["sv_partidas"] == 0  # la reserva se ha devuelto


def test_google_error_mid_round_keeps_round_open(sv_client):
    google = FakeGoogle()
    c = sv_client(google=google)
    game = _new(c)
    google.fail = True
    r = _answer(c, game, 0)
    assert r.status_code == 503 and "no responde" in r.json()["detail"]
    state = c.get("/api/geo/partidas/actual", headers=auth()).json()["partida"]
    assert state["ronda_actual"] == 0  # la ronda sigue abierta
    google.fail = False
    assert _answer(c, game, 0).status_code == 200


def test_maps_loads_cap(sv_client):
    c = sv_client(maps_loads_per_day=3)
    assert _new(c, "ana")["fuente"] == "streetview"  # mapa y panorámica: 2
    assert _new(c, "luis")["fuente"] == "streetview"  # 3: cabe una carga más (y la panorámica se cuenta aparte)
    r = c.post("/api/geo/partidas", json={}, headers=auth("eva"))
    assert r.status_code == 503 and "mapa" in r.json()["detail"]


def test_admin_quota_overrides_are_known(sv_client):
    from meaculpa.geo.router import geo_quota_defaults

    defaults = geo_quota_defaults(GeoSettings(photos_dir=".", quota_sv_games_per_day=3))
    assert defaults["geo_sv_partidas"] == 3 and defaults["geo_sv_global"] == 14 and defaults["geo_mapas"] == 240


# ------------------------------------------------------------------ la clave del servidor no sale
def test_server_key_never_leaves_backend(sv_client, caplog, tmp_path):
    caplog.set_level(logging.DEBUG)
    google = FakeGoogle()
    c = sv_client(google=google)
    bodies = [c.get("/api/geo/models").text]
    game = _new(c)
    bodies.append(json.dumps(game))
    for n in range(5):
        bodies.append(_answer(c, game, n).text)
    bodies.append(c.get(f"/api/geo/partidas/{game['id']}", headers=auth()).text)
    bodies.append(c.get("/api/geo/costes", headers=auth()).text)
    google.fail = True  # el error de red lleva la URL con la clave: no puede llegar ni al cliente ni al registro
    g2 = _new(c, "luis")
    bodies.append(json.dumps(g2))
    bodies.append(c.get("/api/geo/partidas/actual", headers=auth()).text)
    assert all(SV_KEY not in b for b in bodies)
    assert SV_KEY not in caplog.text
    blob = b"".join(p.read_bytes() for p in (tmp_path / "store").rglob("*") if p.is_file())
    assert SV_KEY.encode() not in blob
    assert SV_KEY not in repr(GeoSettings(photos_dir=".", sv_key=SV_KEY))


def test_geo_game_reads_old_records():
    """Las partidas guardadas antes de Street View (sin fuente ni ubicaciones) se siguen leyendo."""
    raw = json.dumps({"id": "a" * 16, "owner": "o", "created": 1.0, "modelo": "m", "fotos": ["x"], "rondas": []})
    g = GeoGame.from_json(raw.encode(), "e")
    assert g.fuente == "fotos" and g.total == 1 and g.ubicaciones == []


def test_ai_clues_only_after_answer_and_kept_on_reload(sv_client):
    class Clues(SpyProvider):
        def locate(self, *args, **kwargs):
            from meaculpa.geo.schemas import GeoGuess
            from meaculpa.llm.base import LLMResult

            self.calls.append((args, kwargs))
            g = GeoGuess(
                pais="Tailandia",
                region="Isan",
                lat=15,
                lon=103,
                confianza=0.4,
                razonamiento_breve="Arrozales y carteles en tailandés.",
                pistas=["Pistaunica carteles en alfabeto tailandés", "Pistaunica circulan por la izquierda"],
            )
            return LLMResult(g, g.model_dump_json(), 1000, 80, 5.0, self.name)

    c = sv_client(spy=Clues())
    game = _new(c)
    assert "Pistaunica" not in json.dumps(game)
    out = _answer(c, game, 0).json()
    assert out["ultima"]["ia"]["pistas"][0].startswith("Pistaunica carteles")
    assert "Pistaunica" not in json.dumps(out["ubicacion_actual"])
    reloaded = c.get("/api/geo/partidas/actual", headers=auth()).json()["partida"]
    assert reloaded["rondas"][0]["ia"]["pistas"] == out["ultima"]["ia"]["pistas"]
