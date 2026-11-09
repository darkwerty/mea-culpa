"""Partidas de Chess.com: lectura de la API pública (falsa), filtros, topes, privacidad y diagnóstico sin ranking."""

from __future__ import annotations

import json
import re
import time

import pytest
from conftest import auth, fake_verifier
from fastapi.testclient import TestClient

from meaculpa.auth import owner_id
from meaculpa.chesscom import (
    USER_AGENT,
    ChessComClient,
    ChessComClosed,
    ChessComError,
    ChessComNotFound,
    ChessComRateLimited,
    _control,
    pick,
    recent_months,
    to_entry,
    valid_username,
)
from meaculpa.importaciones import MAX_GAMES
from meaculpa.llm import MockProvider
from meaculpa.main import create_app

API = "https://api.chess.com/pub"
FOOL = '[Event "Live Chess"]\n[Result "0-1"]\n\n1. f3 e5 2. g4 Qh4# 0-1\n'
SHORT = '[Event "Live Chess"]\n\n1. e4 {[%clk 0:02:59.9]} e5 {[%clk 0:02:58]} 2. Nf3 Nc6 *\n'
NOW = time.mktime((2026, 11, 9, 12, 0, 0, 0, 0, -1))


def game(n, *, white="Ana_99", black="rival1", wres="win", bres="resigned", rules="chess", pgn=SHORT, end=None, **kw):
    return {
        "url": f"https://www.chess.com/game/live/{1000 + n}",
        "pgn": pgn,
        "time_control": "180+2",
        "time_class": "blitz",
        "rules": rules,
        "end_time": end if end is not None else int(NOW) - n * 600,
        "white": {"username": white, "rating": 1500, "result": wres},
        "black": {"username": black, "rating": 1480, "result": bres},
        **kw,
    }


class FakeChessCom:
    """Respuestas de la API por URL. Apunta cada petición (y si alguna llega a la vez que otra).

    `months`: partidas por mes de «ana_99»; `others`: los meses de otros usuarios.
    """

    def __init__(self, months=None, profile=None, status=None, others=None):
        self.months = months if months is not None else {(2026, 11): [game(i) for i in range(5)]}
        self.profile = profile if profile is not None else {"username": "ana_99", "status": "basic"}
        self.status = status or {}  # url -> código
        self.others = others or {}
        self.calls: list[str] = []
        self.active = 0
        self.max_active = 0

    def _months(self, user):
        return self.months if user == "ana_99" else self.others.get(user)

    def __call__(self, url, timeout):
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            self.calls.append(url)
            assert timeout <= 12
            if url in self.status:
                return self.status[url], None
            m = re.fullmatch(rf"{API}/player/([a-z0-9_-]+)(/games/archives|/games/(\d{{4}})/(\d{{2}}))?", url)
            if not m:
                return 404, None
            user, rest = m.group(1), m.group(2)
            months = self._months(user)
            if months is None or (user == "ana_99" and not self.profile):
                return 404, None
            if rest is None:
                return 200, self.profile if user == "ana_99" else {"username": user, "status": "basic"}
            if rest == "/games/archives":
                return 200, {"archives": [f"{API}/player/{user}/games/{y}/{mo:02d}" for y, mo in sorted(months)]}
            key = (int(m.group(3)), int(m.group(4)))
            return (200, {"games": months[key]}) if key in months else (404, None)
        finally:
            self.active -= 1


@pytest.fixture
def fake():
    return FakeChessCom()


@pytest.fixture
def client_for(settings, fake):
    app = create_app(
        settings=settings, provider=MockProvider(seed=5), token_verifier=fake_verifier, chesscom=ChessComClient(fake)
    )

    def _make(user: str | None = "ana") -> TestClient:
        return TestClient(app, headers=auth(user) if user else None)

    return _make


def _store(settings):
    return settings.data_dir / "store"


def _age_import(settings, user="ana", seconds=3600):
    """Simula que la última importación fue hace `seconds` (para saltarse la espera entre importaciones)."""
    path = _store(settings) / "chesscom" / f"{owner_id(user)}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["actualizado"] -= seconds
    path.write_text(json.dumps(data), encoding="utf-8")


# ------------------------------------------------------------------ lectura de cada partida


def test_username_validation():
    assert valid_username(" Hikaru ") == "Hikaru"
    assert valid_username("qwerty0x") == "qwerty0x"
    assert valid_username("a_b-c") == "a_b-c"
    for bad in ["ab", "a" * 26, "../admin", "ana/../x", "ana?x=1", "ana 99", "ána", "", None]:
        assert valid_username(bad) is None


def test_entry_detects_colour_result_and_reason():
    win = to_entry(game(1, wres="win", bres="resigned"), "ana_99", "o" * 16)
    assert win["user_color"] == "white" and win["resultado_usuario"] == "victoria"
    assert win["motivo_fin"] == "abandono del rival" and win["result"] == "1-0"
    assert win["rival"] == "rival1" and win["elo"] == 1500 and win["rival_elo"] == 1480
    assert win["ritmo"] == "blitz" and win["control"] == "3+2" and win["moves"][:2] == ["e2e4", "e7e5"]

    mate = to_entry(game(2, white="rival1", black="ANA_99", wres="win", bres="checkmated"), "ana_99", "o" * 16)
    assert mate["user_color"] == "black" and mate["resultado_usuario"] == "derrota"
    assert mate["motivo_fin"] == "jaque mate" and mate["result"] == "1-0"

    black_win = to_entry(game(3, white="rival1", black="ana_99", wres="timeout", bres="win"), "ana_99", "o" * 16)
    assert black_win["resultado_usuario"] == "victoria" and black_win["result"] == "0-1"

    draw = to_entry(game(4, wres="repetition", bres="repetition"), "ana_99", "o" * 16)
    assert draw["resultado_usuario"] == "tablas" and draw["result"] == "1/2-1/2"
    assert draw["motivo_fin"] == "triple repetición"


def test_entry_skips_variants_foreign_and_broken_games():
    owner = "o" * 16
    assert to_entry(game(1, rules="chess960"), "ana_99", owner) == "variantes"
    odd = game(2, initial_setup="rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w - - 0 1")
    assert to_entry(odd, "ana_99", owner) == "variantes"
    assert to_entry(game(3, white="otro", black="rival1"), "ana_99", owner) == "ajenas"
    assert to_entry(game(4, pgn="1. e4 Ke7?? 2. Qxx9"), "ana_99", owner) == "sin_jugadas"
    assert to_entry(game(5, pgn='[Event "x"]\n\n1. e4 *'), "ana_99", owner) == "sin_jugadas"
    assert to_entry(game(6, url="https://evil.example/game/live/1"), "ana_99", owner) == "sin_jugadas"
    assert to_entry(game(7, pgn="x" * 70_000), "ana_99", owner) == "sin_jugadas"
    bad_name = to_entry(game(8, black="<script>"), "ana_99", owner)
    assert bad_name["rival"] == "rival"


def test_pick_keeps_most_recent_without_duplicates():
    games = [game(i) for i in range(10)] + [game(3)] + [game(20, rules="bughouse")]
    chosen, skipped = pick(games, "ana_99", "o" * 16, 4)
    assert [g["url"][-4:] for g in chosen] == ["1000", "1001", "1002", "1003"]
    assert skipped.variantes == 1


def test_time_controls_and_months():
    assert _control("180+2") == "3+2" and _control("600") == "10 min" and _control("90+1") == "1,5+1"
    assert _control("1/259200") == "3 días por jugada" and _control("-") is None
    assert recent_months(NOW, 3) == [(2026, 11), (2026, 10), (2026, 9)]
    assert recent_months(time.mktime((2026, 1, 15, 0, 0, 0, 0, 0, -1)), 2) == [(2026, 1), (2025, 12)]


# ------------------------------------------------------------------ cliente de la API


def test_client_reads_only_recent_months_of_that_user():
    fake = FakeChessCom(
        months={
            (2026, 11): [game(1)],
            (2026, 10): [game(2)],
            (2026, 9): [game(3)],
            (2026, 8): [game(4)],
        }
    )
    raw, months = ChessComClient(fake).recent_games("Ana_99", NOW, 3, 100)
    assert months == [(2026, 11), (2026, 10), (2026, 9)]
    assert f"{API}/player/ana_99/games/2026/08" not in fake.calls
    assert len(raw) == 3 and fake.max_active == 1


def test_client_ignores_archive_urls_of_other_hosts_or_users():
    fake = FakeChessCom()

    def evil(url, timeout):
        if url.endswith("/archives"):
            return 200, {"archives": [f"{API}/player/otro/games/2026/11", "https://evil.example/x/2026/11"]}
        return fake(url, timeout)

    raw, months = ChessComClient(evil).recent_games("ana_99", NOW, 3, 100)
    assert raw == [] and months == []


def test_client_errors():
    with pytest.raises(ChessComNotFound, match="No hay ningún usuario"):
        ChessComClient(FakeChessCom(profile={})).recent_games("ana_99", NOW, 3, 100)
    with pytest.raises(ChessComClosed, match="cerrada"):
        ChessComClient(FakeChessCom(profile={"status": "closed:fair_play_violations"})).recent_games(
            "ana_99", NOW, 3, 100
        )
    limited = FakeChessCom(status={f"{API}/player/ana_99/games/archives": 429})
    with pytest.raises(ChessComRateLimited):
        ChessComClient(limited).recent_games("ana_99", NOW, 3, 100)
    assert len(limited.calls) == 2  # sin reintentos tras un 429

    def broken(url, timeout):
        raise TimeoutError("lento")

    with pytest.raises(ChessComError, match="no ha respondido"):
        ChessComClient(broken).recent_games("ana_99", NOW, 3, 100)


def test_client_total_deadline():
    ticks = iter([0, 0, 0, 40, 40, 40])
    fake = FakeChessCom()
    with pytest.raises(ChessComError, match="tardando"):
        ChessComClient(fake, clock=lambda: next(ticks)).recent_games("ana_99", NOW, 3, 100)


def test_user_agent_names_the_site_and_only_the_public_contact():
    assert "https://danielalonso.dev/meaculpa" in USER_AGENT
    assert "hola@danielalonso.dev" in USER_AGENT and USER_AGENT.count("@") == 1


# ------------------------------------------------------------------ API


def test_routes_require_login(client_for):
    anon = client_for(None)
    assert anon.get("/api/meaculpa/chesscom").status_code == 401
    assert anon.post("/api/meaculpa/chesscom", json={"usuario": "ana_99"}).status_code == 401
    assert anon.delete("/api/meaculpa/chesscom").status_code == 401
    assert anon.get("/api/meaculpa/chesscom/partidas/0123456789abcdef").status_code == 401
    assert anon.post("/api/meaculpa/chesscom/partidas/0123456789abcdef/analysis").status_code == 401
    assert anon.get("/api/meaculpa/chesscom/partidas/0123456789abcdef/analysis").status_code == 401


def test_connect_lists_and_opens_games(client_for, fake):
    ana = client_for("ana")
    assert ana.get("/api/meaculpa/chesscom").json()["usuario"] is None
    res = ana.post("/api/meaculpa/chesscom", json={"usuario": "Ana_99"})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["usuario"] == "Ana_99" and len(body["partidas"]) == 5 and body["siguiente"]
    assert "5 partidas de Ana_99" in body["mensaje"]
    first = body["partidas"][0]
    assert "moves" not in first and first["jugadas"] == 4 and first["rival"] == "rival1"
    listed = ana.get("/api/meaculpa/chesscom").json()
    assert [g["id"] for g in listed["partidas"]] == [g["id"] for g in body["partidas"]]
    state = ana.get(f"/api/meaculpa/chesscom/partidas/{first['id']}").json()
    assert state["origen"] == "chesscom" and state["status"] == "terminada" and state["legal_moves"] == []
    assert state["moves_san"] == ["e4", "e5", "Nf3", "Nc6"] and state["externo"]["usuario"] == "Ana_99"


def test_connect_validates_and_reports_chesscom_problems(settings, client_for, fake):
    ana = client_for("ana")
    assert ana.post("/api/meaculpa/chesscom", json={"usuario": "../x"}).status_code == 422
    assert not fake.calls  # con un nombre no válido no se llama a Chess.com

    fake.profile = {}
    res = ana.post("/api/meaculpa/chesscom", json={"usuario": "ana_99"})
    assert res.status_code == 404 and "No hay ningún usuario" in res.json()["detail"]

    fake.profile = {"status": "basic"}
    fake.months = {}
    res = ana.post("/api/meaculpa/chesscom", json={"usuario": "ana_99"})
    assert res.status_code == 404 and "no tiene partidas" in res.json()["detail"]

    fake.months = {(2026, 11): [game(1, rules="chess960")]}
    res = ana.post("/api/meaculpa/chesscom", json={"usuario": "ana_99"})
    assert res.status_code == 404 and "variantes" in res.json()["detail"]

    fake.months = {(2026, 11): [game(1)]}
    fake.status = {f"{API}/player/ana_99": 429}
    res = ana.post("/api/meaculpa/chesscom", json={"usuario": "ana_99"})
    assert res.status_code == 503 and "esperemos" in res.json()["detail"]
    assert ana.get("/api/meaculpa/chesscom").json()["usuario"] is None


def test_daily_quota_counts_not_found_but_refunds_chesscom_failures(settings, client_for, fake):
    import dataclasses

    from meaculpa.quota import UsageLimiter
    from meaculpa.storage import FileObjectStore

    ana = client_for("ana")
    fake.status = {f"{API}/player/ana_99": 500}
    assert ana.post("/api/meaculpa/chesscom", json={"usuario": "ana_99"}).status_code == 503
    fake.status = {}
    fake.profile = {}
    assert ana.post("/api/meaculpa/chesscom", json={"usuario": "ana_99"}).status_code == 404
    limiter = UsageLimiter(FileObjectStore(_store(settings)), dataclasses.replace(settings))
    day = time.strftime("%Y-%m-%d", time.gmtime())
    raw = (_store(settings) / "usage" / day / owner_id("ana")).read_text(encoding="utf-8")
    assert json.loads(raw)["importaciones"] == 1
    assert limiter.limit("importaciones") == settings.quota_imports_per_day


def test_daily_import_quota(settings, client_for, fake):
    import dataclasses

    app = create_app(
        settings=dataclasses.replace(settings, quota_imports_per_day=1),
        provider=MockProvider(seed=5),
        token_verifier=fake_verifier,
        chesscom=ChessComClient(fake),
    )
    ana = TestClient(app, headers=auth("ana"))
    assert ana.post("/api/meaculpa/chesscom", json={"usuario": "ana_99"}).status_code == 200
    _age_import(settings)
    res = ana.post("/api/meaculpa/chesscom", json={"usuario": "ana_99"})
    assert res.status_code == 429 and "importaciones de Chess.com" in res.json()["detail"]


def test_cooldown_and_refresh_without_duplicates(settings, client_for, fake):
    ana = client_for("ana")
    first = ana.post("/api/meaculpa/chesscom", json={"usuario": "ana_99"}).json()
    res = ana.post("/api/meaculpa/chesscom", json={"usuario": "ana_99"})
    assert res.status_code == 429 and "dentro de 5 minutos" in res.json()["detail"]
    _age_import(settings)
    fake.months[(2026, 11)] = [game(-1), *fake.months[(2026, 11)]]  # una nueva
    again = ana.post("/api/meaculpa/chesscom", json={"usuario": "ana_99"}).json()
    assert again["nuevas"] == 1 and len(again["partidas"]) == 6
    assert "1 partida nueva" in again["mensaje"]
    assert {g["id"] for g in first["partidas"]} < {g["id"] for g in again["partidas"]}


def test_at_most_max_games_most_recent_first(settings, client_for, fake):
    fake.months = {
        (2026, 11): [game(i) for i in range(70)],
        (2026, 10): [game(100 + i, end=int(NOW) - 40 * 86400 - i) for i in range(70)],
    }
    body = client_for("ana").post("/api/meaculpa/chesscom", json={"usuario": "ana_99"}).json()
    assert len(body["partidas"]) == MAX_GAMES
    created = [g["created"] for g in body["partidas"]]
    assert created == sorted(created, reverse=True)
    assert body["meses_leidos"] == ["2026-11", "2026-10"]


def test_games_are_private_to_their_owner(client_for):
    ana, bea = client_for("ana"), client_for("bea")
    gid = ana.post("/api/meaculpa/chesscom", json={"usuario": "ana_99"}).json()["partidas"][0]["id"]
    assert bea.get("/api/meaculpa/chesscom").json()["partidas"] == []
    assert bea.get(f"/api/meaculpa/chesscom/partidas/{gid}").status_code == 404
    assert bea.post(f"/api/meaculpa/chesscom/partidas/{gid}/analysis").status_code == 404
    assert bea.get(f"/api/meaculpa/chesscom/partidas/{gid}/analysis").status_code == 404
    assert bea.delete("/api/meaculpa/chesscom").json()["borradas"] == 0
    assert len(ana.get("/api/meaculpa/chesscom").json()["partidas"]) == 5
    # Tampoco son partidas de Mea culpa: no se abren ni se juegan por las rutas de siempre.
    assert ana.get(f"/api/meaculpa/games/{gid}").status_code == 404
    assert ana.post(f"/api/meaculpa/games/{gid}/move", json={"uci": "e2e4"}).status_code == 404
    assert ana.get("/api/meaculpa/games").json() == []


def test_diagnosis_uses_quota_and_never_reaches_rankings(settings, client_for, fake):
    fake.months = {(2026, 11): [game(1, white="rival1", black="ana_99", wres="win", bres="checkmated", pgn=FOOL)]}
    ana = client_for("ana")
    gid = ana.post("/api/meaculpa/chesscom", json={"usuario": "ana_99"}).json()["partidas"][0]["id"]
    assert ana.get(f"/api/meaculpa/chesscom/partidas/{gid}/analysis").status_code == 404
    res = ana.post(f"/api/meaculpa/chesscom/partidas/{gid}/analysis")
    assert res.status_code == 200, res.text
    diag = res.json()
    assert diag["game_id"] == gid and diag["diagnostico"]["titular"]
    assert ana.get(f"/api/meaculpa/chesscom/partidas/{gid}/analysis").json() == diag
    row = ana.get("/api/meaculpa/chesscom").json()["partidas"][0]
    assert row["analizada"] and row["titular"] == diag["diagnostico"]["titular"]
    assert ana.get(f"/api/meaculpa/chesscom/partidas/{gid}").json()["has_diagnosis"]

    day = time.strftime("%Y-%m-%d", time.gmtime())
    usage = json.loads((_store(settings) / "usage" / day / owner_id("ana")).read_text(encoding="utf-8"))
    assert usage["analisis"] == 1

    ranking = ana.get("/api/ranking/meaculpa")
    assert ranking.status_code == 200
    assert "Ana" not in ranking.text and gid not in ranking.text and "rival1" not in ranking.text
    db = ana.app.state.data.db
    db.flush()
    assert db.query("SELECT id FROM partidas") == []  # ni siquiera en la copia SQL de /admin


def test_diagnosis_respects_analysis_quota(settings, fake):
    import dataclasses

    fake.months = {(2026, 11): [game(1, pgn=FOOL, white="ana_99", black="rival1", wres="checkmated", bres="win")]}
    app = create_app(
        settings=dataclasses.replace(settings, quota_analyses_per_day=0),
        provider=MockProvider(seed=5),
        token_verifier=fake_verifier,
        chesscom=ChessComClient(fake),
    )
    ana = TestClient(app, headers=auth("ana"))
    gid = ana.post("/api/meaculpa/chesscom", json={"usuario": "ana_99"}).json()["partidas"][0]["id"]
    res = ana.post(f"/api/meaculpa/chesscom/partidas/{gid}/analysis")
    assert res.status_code == 429 and "análisis" in res.json()["detail"]


def test_disconnect_deletes_everything(settings, client_for, fake):
    fake.months = {(2026, 11): [game(1, pgn=FOOL, white="ana_99", black="rival1", wres="checkmated", bres="win")]}
    ana = client_for("ana")
    gid = ana.post("/api/meaculpa/chesscom", json={"usuario": "ana_99"}).json()["partidas"][0]["id"]
    assert ana.post(f"/api/meaculpa/chesscom/partidas/{gid}/analysis").status_code == 200
    assert list((_store(settings) / "chesscom-analisis").rglob("*.json"))
    assert ana.delete("/api/meaculpa/chesscom").json() == {"borradas": 1}
    assert ana.get("/api/meaculpa/chesscom").json() == {
        "usuario": None,
        "actualizado": None,
        "siguiente": None,
        "meses": 3,
        "maximo": MAX_GAMES,
        "partidas": [],
    }
    assert not list((_store(settings) / "chesscom-analisis").rglob("*.json"))
    assert not list((_store(settings) / "chesscom").rglob("*.json"))
    # Tras borrar se puede volver a conectar sin esperar.
    assert ana.post("/api/meaculpa/chesscom", json={"usuario": "ana_99"}).status_code == 200


def test_other_username_replaces_previous_games(settings, client_for, fake):
    fake.months = {(2026, 11): [game(1, pgn=FOOL, white="ana_99", black="rival1", wres="checkmated", bres="win")]}
    fake.others = {"luis_7": {(2026, 11): [game(50, white="Luis_7", black="rival2")]}}
    ana = client_for("ana")
    gid = ana.post("/api/meaculpa/chesscom", json={"usuario": "ana_99"}).json()["partidas"][0]["id"]
    assert ana.post(f"/api/meaculpa/chesscom/partidas/{gid}/analysis").status_code == 200
    _age_import(settings)
    body = ana.post("/api/meaculpa/chesscom", json={"usuario": "Luis_7"}).json()
    assert body["usuario"] == "Luis_7" and [g["rival"] for g in body["partidas"]] == ["rival2"]
    assert ana.get(f"/api/meaculpa/chesscom/partidas/{gid}").status_code == 404
    assert not list((_store(settings) / "chesscom-analisis").rglob("*.json"))


def test_stored_data_has_no_email_or_raw_sub(settings, client_for):
    client_for("1234567890").post("/api/meaculpa/chesscom", json={"usuario": "ana_99"})
    files = list((_store(settings) / "chesscom").rglob("*.json"))
    assert len(files) == 1
    text = files[0].read_text(encoding="utf-8")
    assert "1234567890" not in files[0].as_posix() and "1234567890" not in text and "@" not in text
    assert "pgn" not in json.loads(text)["partidas"][0]  # solo las jugadas, sin el PGN con sus cabeceras
