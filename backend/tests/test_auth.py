"""Login con Google: verificación real del ID token (firma RSA, aud, iss, exp, email_verified)."""

from __future__ import annotations

import dataclasses
import json
import time

import pytest
from conftest import TEST_CLIENT_ID
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from google.auth import crypt, jwt

from meaculpa.auth import google_verifier, owner_id
from meaculpa.config import parse_cors_origins
from meaculpa.llm import MockProvider
from meaculpa.main import create_app

KID = "clave-test"
PROTECTED = [
    ("GET", "/api/opponents"),
    ("GET", "/api/games"),
    ("POST", "/api/games"),
    ("GET", "/api/games/abcdef123456"),
    ("POST", "/api/games/abcdef123456/move"),
    ("POST", "/api/games/abcdef123456/resign"),
    ("POST", "/api/games/abcdef123456/analysis"),
    ("GET", "/api/games/abcdef123456/analysis"),
    ("POST", "/api/analysis/pgn"),
    ("GET", "/api/metrics/llm"),
]


def _signer(key) -> crypt.RSASigner:
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    return crypt.RSASigner.from_string(pem, key_id=KID)


@pytest.fixture(scope="module")
def google_keys():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_pem = key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return _signer(key), public_pem.decode()


class FakeCertsResponse:
    def __init__(self, certs: dict[str, str]) -> None:
        self.status = 200
        self.headers: dict[str, str] = {}
        self.data = json.dumps(certs).encode()


def _token(signer, **overrides) -> str:
    now = int(time.time())
    claims = {
        "iss": "https://accounts.google.com",
        "aud": TEST_CLIENT_ID,
        "azp": TEST_CLIENT_ID,
        "sub": "1122334455",
        "email": "ana@example.com",
        "email_verified": True,
        "given_name": "Ana",
        "iat": now,
        "exp": now + 3600,
    }
    claims.update(overrides)
    return jwt.encode(signer, claims).decode()


@pytest.fixture
def google_client(settings, google_keys):
    _, public_pem = google_keys

    def fake_request(url, method="GET", **kwargs):
        return FakeCertsResponse({KID: public_pem})

    verifier = google_verifier(TEST_CLIENT_ID, request=fake_request)
    app = create_app(settings=settings, provider=MockProvider(seed=1), token_verifier=verifier)
    return TestClient(app)


def _get(client, token):
    return client.get("/api/opponents", headers={"Authorization": f"Bearer {token}"})


def test_valid_google_token_is_accepted(google_client, google_keys):
    signer, _ = google_keys
    res = _get(google_client, _token(signer))
    assert res.status_code == 200, res.text
    assert res.json()["usuario"] == "Ana"
    # El propietario es un hash del sub: ni el sub ni el correo en claro.
    assert len(owner_id("1122334455")) == 64 and "1122334455" not in owner_id("1122334455")


@pytest.mark.parametrize(
    "overrides",
    [
        {"aud": "otro-cliente.apps.googleusercontent.com"},
        {"iss": "https://evil.example"},
        {"exp": int(time.time()) - 600, "iat": int(time.time()) - 4200},
        {"email_verified": False},
        {"email_verified": "true"},
        {"sub": ""},
    ],
    ids=["aud", "iss", "exp", "email_no_verificado", "email_verified_texto", "sin_sub"],
)
def test_invalid_google_tokens_are_rejected(google_client, google_keys, overrides):
    signer, _ = google_keys
    res = _get(google_client, _token(signer, **overrides))
    assert res.status_code == 401
    assert res.headers["WWW-Authenticate"] == "Bearer"


def test_token_signed_with_another_key_is_rejected(google_client):
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    assert _get(google_client, _token(_signer(other))).status_code == 401


def test_verifier_refuses_empty_client_id():
    with pytest.raises(ValueError):
        google_verifier("")


@pytest.mark.parametrize(("method", "path"), PROTECTED)
def test_every_route_but_health_requires_login(settings, make_client, method, path):
    client = make_client(settings, user=None)
    assert client.request(method, path, json={}).status_code == 401
    assert client.request(method, path, json={}, headers={"Authorization": "Basic abc"}).status_code == 401
    assert client.request(method, path, json={}, headers={"Authorization": "Bearer x:firma"}).status_code == 401
    assert client.get("/api/health").status_code == 200


@pytest.mark.parametrize("defect", ["aud", "iss", "exp", "email", "firma"])
def test_claims_are_checked_even_if_the_verifier_lets_them_through(settings, make_client, defect):
    client = make_client(settings, user=f"ana:{defect}")
    assert client.get("/api/games").status_code == 401


def test_api_does_not_start_without_google_client_id(settings):
    with pytest.raises(RuntimeError, match="GOOGLE_CLIENT_ID"):
        create_app(settings=dataclasses.replace(settings, google_client_id=None), provider=MockProvider())


def test_docs_and_openapi_are_not_published(settings, make_client):
    client = make_client(settings)
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404


def test_security_headers_and_cors(settings, make_client):
    client = make_client(dataclasses.replace(settings, cors_origins=("https://meaculpa.danielalonso.dev",)))
    res = client.get("/api/health")
    assert res.headers["X-Content-Type-Options"] == "nosniff"
    assert "frame-ancestors 'none'" in res.headers["Content-Security-Policy"]
    preflight = {
        "Origin": "https://meaculpa.danielalonso.dev",
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "authorization,content-type",
    }
    ok = client.options("/api/games", headers=preflight)
    assert ok.status_code == 200
    assert ok.headers["access-control-allow-origin"] == "https://meaculpa.danielalonso.dev"
    evil = client.options("/api/games", headers={**preflight, "Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in evil.headers


def test_cors_rejects_wildcards():
    assert parse_cors_origins("https://meaculpa.danielalonso.dev, http://localhost:3000/") == (
        "https://meaculpa.danielalonso.dev",
        "http://localhost:3000",
    )
    for bad in ("*", "https://*.azurestaticapps.net", "http://evil.example"):
        with pytest.raises(ValueError):
            parse_cors_origins(bad)
