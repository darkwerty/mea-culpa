"""Panel /api/admin, controles que respetan las demos y formulario de contacto (sin red: SQLite en memoria)."""

from __future__ import annotations

import time

import pytest
from conftest import TEST_CLIENT_ID
from fastapi.testclient import TestClient

from meaculpa.auth import owner_id
from meaculpa.costes_azure import AzureCosts, parse_query
from meaculpa.db import Database, split_statements
from meaculpa.llm import MockProvider
from meaculpa.main import create_app

ADMIN = "admin@example.com"
COST_BODY = {
    "properties": {
        "columns": [{"name": "Cost"}, {"name": "ServiceName"}, {"name": "Currency"}],
        "rows": [[1.5, "Container Apps", "EUR"], [0.25, "Storage", "EUR"]],
    }
}


def verifier(token: str) -> dict:
    """Token de prueba: '<sub>|<correo>|<verificado: 1 o 0>'."""
    sub, email, verified = (token.split("|") + ["", "1"])[:3]
    claims = {
        "aud": TEST_CLIENT_ID,
        "iss": "https://accounts.google.com",
        "exp": time.time() + 3600,
        "sub": sub,
        "email_verified": verified == "1",
        "given_name": sub.capitalize(),
    }
    if email:
        claims["email"] = email
    return claims


def tok(sub: str, email: str = "", verified: bool = True) -> dict[str, str]:
    return {"Authorization": f"Bearer {sub}|{email}|{'1' if verified else '0'}"}


ADMIN_H = tok("dani", ADMIN)
ANA_H = tok("ana", "ana@example.com")


@pytest.fixture
def api(settings):
    calls = {"n": 0}

    def fetch():
        calls["n"] += 1
        return COST_BODY

    app = create_app(
        settings=settings,
        provider=MockProvider(seed=5),
        token_verifier=verifier,
        database=Database.sqlite(),
        azure_costs=AzureCosts(fetch, 40.0),
    )
    client = TestClient(app)
    client.cost_calls = calls
    return client


ADMIN_GETS = [
    "/yo",
    "/resumen",
    "/usuarios",
    "/uso",
    "/partidas",
    "/gasto-ia",
    "/gasto-azure",
    "/mensajes",
    "/controles",
]


def test_admin_requires_login(api):
    for path in ADMIN_GETS:
        assert api.get("/api/admin" + path).status_code == 401


def test_non_admin_gets_403_everywhere(api):
    others = [
        ANA_H,
        tok("x", "admin@example.com.evil.com"),
        tok("y", "otro" + ADMIN),
        tok("z"),  # sin correo
    ]
    for headers in others:
        for path in ADMIN_GETS:
            assert api.get("/api/admin" + path, headers=headers).status_code == 403, (headers, path)
        assert (
            api.post("/api/admin/controles/demos/meaculpa", json={"activo": False}, headers=headers).status_code == 403
        )
        assert api.delete("/api/admin/mensajes/abc", headers=headers).status_code == 403


def test_admin_email_must_be_verified(api):
    # Correo sin verificar: el token entero se rechaza (401), aunque el correo sea el del admin.
    assert api.get("/api/admin/yo", headers=tok("dani", ADMIN, verified=False)).status_code == 401


def test_admin_email_case_insensitive_and_allowed(api):
    res = api.get("/api/admin/yo", headers=tok("dani", "admin@example.com"))
    assert res.status_code == 200 and res.json()["email"] == ADMIN


def test_admin_emails_setting(settings):
    import dataclasses

    s = dataclasses.replace(settings, admin_emails=("otra@example.com",))
    client = TestClient(create_app(settings=s, token_verifier=verifier, database=Database.sqlite()))
    assert client.get("/api/admin/yo", headers=ADMIN_H).status_code == 403
    assert client.get("/api/admin/yo", headers=tok("o", "otra@example.com")).status_code == 200


def test_usage_users_and_ai_spend_are_recorded(api):
    gid = api.post("/api/meaculpa/games", json={"opponent": {"type": "llm"}}, headers=ANA_H).json()["id"]
    state = api.get(f"/api/meaculpa/games/{gid}", headers=ANA_H).json()
    api.post(f"/api/meaculpa/games/{gid}/move", json={"uci": state["legal_moves"][0]}, headers=ANA_H)
    users = api.get("/api/admin/usuarios", headers=ADMIN_H).json()["usuarios"]
    ana = next(u for u in users if u["owner"] == owner_id("ana"))
    assert ana["email"] == "ana@example.com" and ana["nombre"] == "Ana" and not ana["bloqueado"]
    assert ana["eventos"] >= 3  # partida, jugada, jugada de la IA
    summary = api.get("/api/admin/resumen", headers=ADMIN_H).json()
    assert summary["usuarios"]["total"] == 2 and summary["partidas_mes"] == 1
    usage = api.get("/api/admin/uso", headers=ADMIN_H).json()["por_dia"]
    assert usage[0]["demo"] == "meaculpa" and usage[0]["eventos"]["partida"] == 1
    games = api.get("/api/admin/partidas", headers=ADMIN_H).json()["partidas"]
    assert games[0]["ref"] == gid and games[0]["jugadas"] == 1 and games[0]["email"] == "ana@example.com"
    spend = api.get("/api/admin/gasto-ia", headers=ADMIN_H).json()
    assert spend["por_modelo"] and spend["por_modelo"][0]["llamadas"] >= 1


def test_azure_spend_is_cached(api):
    first = api.get("/api/admin/gasto-azure", headers=ADMIN_H).json()
    api.get("/api/admin/gasto-azure", headers=ADMIN_H)
    assert first["total"] == 1.75 and first["presupuesto"] == 40.0 and first["porcentaje"] == 4.4
    assert first["por_servicio"][0]["servicio"] == "Container Apps"
    assert api.cost_calls["n"] == 1


def test_azure_spend_without_subscription():
    assert AzureCosts(None, 40).month()["disponible"] is False
    assert parse_query({"properties": {"columns": [{"name": "Cost"}], "rows": []}})[0] == 0


def test_toggle_demo(api):
    assert api.post("/api/admin/controles/demos/meaculpa", json={"activo": False}, headers=ADMIN_H).status_code == 200
    res = api.post("/api/meaculpa/games", json={"opponent": {"type": "random"}}, headers=ANA_H)
    assert res.status_code == 503
    assert api.get("/api/meaculpa/models").json()["demo_activa"] is False
    assert api.get("/api/meaculpa/health").status_code == 200
    assert api.get("/api/geo/costes", headers=ANA_H).status_code == 200  # la otra demo sigue
    api.post("/api/admin/controles/demos/geo", json={"activo": False}, headers=ADMIN_H)
    assert api.get("/api/geo/costes", headers=ANA_H).status_code == 503
    api.post("/api/admin/controles/demos/meaculpa", json={"activo": True}, headers=ADMIN_H)
    assert api.post("/api/meaculpa/games", json={"opponent": {"type": "random"}}, headers=ANA_H).status_code == 200
    assert api.post("/api/admin/controles/demos/otra", json={"activo": False}, headers=ADMIN_H).status_code == 422


def test_toggle_model(api):
    models = api.get("/api/meaculpa/models").json()
    mid = models["por_defecto"]
    assert api.post(f"/api/admin/controles/modelos/{mid}", json={"activo": False}, headers=ADMIN_H).status_code == 200
    assert next(m for m in api.get("/api/meaculpa/models").json()["modelos"] if m["id"] == mid)["disponible"] is False
    res = api.post("/api/meaculpa/games", json={"opponent": {"type": "llm", "model": mid}}, headers=ANA_H)
    assert res.status_code == 400
    ctl = api.get("/api/admin/controles", headers=ADMIN_H).json()
    assert next(m for m in ctl["modelos"] if m["id"] == mid)["activo"] is False
    api.post(f"/api/admin/controles/modelos/{mid}", json={"activo": True}, headers=ADMIN_H)
    res = api.post("/api/meaculpa/games", json={"opponent": {"type": "llm", "model": mid}}, headers=ANA_H)
    assert res.status_code == 200
    assert api.post("/api/admin/controles/modelos/nada", json={"activo": False}, headers=ADMIN_H).status_code == 404


def test_change_quota(api):
    res = api.post("/api/admin/controles/cuotas", json={"tipo": "partidas", "valor": 1}, headers=ADMIN_H)
    assert res.status_code == 200 and res.json()["valor"] == 1
    body = {"opponent": {"type": "random"}}
    assert api.post("/api/meaculpa/games", json=body, headers=ANA_H).status_code == 200
    assert api.post("/api/meaculpa/games", json=body, headers=ANA_H).status_code == 429
    quota = next(
        q for q in api.get("/api/admin/controles", headers=ADMIN_H).json()["cuotas"] if q["tipo"] == "partidas"
    )
    assert quota["valor"] == 1 and quota["cambiada"] and quota["por_defecto"] == 30
    api.post("/api/admin/controles/cuotas", json={"tipo": "partidas", "valor": None}, headers=ADMIN_H)
    assert api.post("/api/meaculpa/games", json=body, headers=ANA_H).status_code == 200
    assert api.post("/api/admin/controles/cuotas", json={"tipo": "x", "valor": 3}, headers=ADMIN_H).status_code == 404
    assert (
        api.post("/api/admin/controles/cuotas", json={"tipo": "partidas", "valor": -1}, headers=ADMIN_H).status_code
        == 422
    )


def test_block_and_unblock_user(api):
    assert api.get("/api/meaculpa/opponents", headers=ANA_H).status_code == 200
    owner = owner_id("ana")
    res = api.post(f"/api/admin/usuarios/{owner}/bloqueo", json={"bloqueado": True}, headers=ADMIN_H)
    assert res.status_code == 200
    assert api.get("/api/meaculpa/opponents", headers=ANA_H).status_code == 403
    assert api.get("/api/geo/costes", headers=ANA_H).status_code == 403
    api.post(f"/api/admin/usuarios/{owner}/bloqueo", json={"bloqueado": False}, headers=ADMIN_H)
    assert api.get("/api/meaculpa/opponents", headers=ANA_H).status_code == 200
    # No se puede bloquear al admin ni a un usuario que no existe.
    api.get("/api/admin/yo", headers=ADMIN_H)
    res = api.post(f"/api/admin/usuarios/{owner_id('dani')}/bloqueo", json={"bloqueado": True}, headers=ADMIN_H)
    assert res.status_code == 409
    res = api.post(f"/api/admin/usuarios/{'0' * 64}/bloqueo", json={"bloqueado": True}, headers=ADMIN_H)
    assert res.status_code == 404


MSG = {"nombre": "Lucía", "email": "lucia@example.com", "mensaje": "Hola, me gustaría hablar de un proyecto."}


def test_contact_form_and_admin_messages(api):
    assert api.post("/api/contacto", json=MSG).json() == {"ok": True}
    msgs = api.get("/api/admin/mensajes", headers=ADMIN_H).json()["mensajes"]
    assert len(msgs) == 1 and msgs[0]["email"] == "lucia@example.com" and not msgs[0]["leido"]
    assert set(msgs[0]) == {"id", "ts", "nombre", "email", "mensaje", "leido"}  # ni IP ni navegador
    assert api.get("/api/admin/resumen", headers=ADMIN_H).json()["mensajes_sin_leer"] == 1
    mid = msgs[0]["id"]
    assert api.post(f"/api/admin/mensajes/{mid}/leido", json={"leido": True}, headers=ADMIN_H).status_code == 200
    assert api.get("/api/admin/mensajes", headers=ADMIN_H).json()["mensajes"][0]["leido"] is True
    assert api.delete(f"/api/admin/mensajes/{mid}", headers=ADMIN_H).status_code == 200
    assert api.get("/api/admin/mensajes", headers=ADMIN_H).json()["mensajes"] == []
    assert api.delete(f"/api/admin/mensajes/{mid}", headers=ADMIN_H).status_code == 404


def test_contact_validation(api):
    bad = [
        {**MSG, "email": "no-es-un-correo"},
        {**MSG, "mensaje": "corto"},
        {**MSG, "nombre": "   "},
        {**MSG, "mensaje": "x" * 4001},
        {"nombre": "a"},
    ]
    for body in bad:
        assert api.post("/api/contacto", json=body).status_code == 422, body


def test_contact_honeypot_is_not_saved(api):
    assert api.post("/api/contacto", json={**MSG, "web": "http://spam.example"}).json() == {"ok": True}
    assert api.get("/api/admin/mensajes", headers=ADMIN_H).json()["mensajes"] == []


def test_contact_rate_limit_per_ip(api):
    for _ in range(5):
        assert api.post("/api/contacto", json=MSG, headers={"X-Forwarded-For": "203.0.113.7"}).status_code == 200
    assert api.post("/api/contacto", json=MSG, headers={"X-Forwarded-For": "203.0.113.7"}).status_code == 429
    assert api.post("/api/contacto", json=MSG, headers={"X-Forwarded-For": "198.51.100.1"}).status_code == 200


def test_contact_old_messages_are_purged(settings):
    from meaculpa.datos import DataStore

    now = {"t": time.time()}
    data = DataStore(Database.sqlite(), clock=lambda: now["t"])
    data.add_message("A", "a@example.com", "mensaje antiguo de prueba")
    now["t"] += 366 * 86400
    data.add_message("B", "b@example.com", "mensaje nuevo de prueba")
    assert [m["nombre"] for m in data.messages()] == ["B"]


def test_migrations_are_idempotent(tmp_path):
    db = Database.sqlite(tmp_path / "x.db")
    assert db.migrate() == []
    assert 1 in db.applied_versions()
    assert all(not s.startswith("--") for s in split_statements("-- a\nCREATE TABLE t (a INT);\n-- b\n"))


def test_geo_round_is_recorded_and_model_toggle(make_geo_client):
    from geo_support import geo_auth
    from test_geo_api import SpyProvider

    client = make_geo_client(models={"spy": SpyProvider()})
    app = client.app
    game = client.post("/api/geo/partidas", json={"modelo": "spy"}, headers=geo_auth("ana")).json()
    client.post(f"/api/geo/partidas/{game['id']}/rondas/0", json={"lat": 1, "lon": 1}, headers=geo_auth("ana"))
    data = app.state.data
    spend = data.ai_spend()
    assert spend["por_modelo"][0]["modelo"] == "spy" and spend["por_modelo"][0]["tokens_entrada"] == 1000
    assert data.recent_games()[0]["jugadas"] == 1 and data.recent_games()[0]["demo"] == "geo"
    data.set_config("modelo.spy", "0")
    assert client.post("/api/geo/partidas", json={"modelo": "spy"}, headers=geo_auth("ana")).status_code == 400
    r = client.post(f"/api/geo/partidas/{game['id']}/rondas/1", json={"lat": 1, "lon": 1}, headers=geo_auth("ana"))
    assert r.status_code == 503
