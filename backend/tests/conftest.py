from __future__ import annotations

import dataclasses
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from meaculpa.config import get_settings
from meaculpa.llm import MockProvider
from meaculpa.main import create_app

PGN_DIR = Path(__file__).resolve().parents[2] / "data" / "pgn"
TEST_CLIENT_ID = "test-client.apps.googleusercontent.com"


@pytest.fixture
def settings(tmp_path):
    return dataclasses.replace(
        get_settings(),
        data_dir=tmp_path,
        analysis_depth=3,
        llm_provider="mock",
        google_client_id=TEST_CLIENT_ID,
        storage_account_url=None,
    )


def fake_verifier(token: str) -> dict:
    """Simula la verificación de Google: el token es '<sub>' o '<sub>:<defecto>'."""
    sub, _, defect = token.partition(":")
    if defect == "firma":
        raise ValueError("firma no válida")
    return {
        "aud": "otro-cliente" if defect == "aud" else TEST_CLIENT_ID,
        "iss": "https://evil.example" if defect == "iss" else "https://accounts.google.com",
        "exp": time.time() - 3600 if defect == "exp" else time.time() + 3600,
        "sub": sub,
        "email_verified": defect != "email",
        "given_name": sub.capitalize(),
    }


def auth(sub: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {sub}"}


@pytest.fixture
def make_client():
    def _make(settings, user: str | None = "ana") -> TestClient:
        app = create_app(settings=settings, provider=MockProvider(seed=5), token_verifier=fake_verifier)
        return TestClient(app, headers=auth(user) if user else None)

    return _make


@pytest.fixture
def pgn_text():
    def _read(name: str) -> str:
        return (PGN_DIR / name).read_text(encoding="utf-8")

    return _read


@pytest.fixture
def make_geo_client(tmp_path):
    """API completa (create_app de Mea culpa) con fotos falsas para el router de ¿Dónde está esta foto?."""
    from geo_support import GEO_CLIENT_ID, geo_verifier, make_photos

    from meaculpa.geo.config import GeoSettings
    from meaculpa.storage import FileObjectStore

    def _make(models=None, **geo_overrides) -> TestClient:
        settings = dataclasses.replace(
            get_settings(),
            google_client_id=GEO_CLIENT_ID,
            llm_provider="mock",
            data_dir=tmp_path / "data",
            storage_account_url=None,
        )
        app = create_app(
            settings=settings,
            provider=MockProvider(seed=5),
            token_verifier=geo_verifier,
            objects=FileObjectStore(tmp_path / "store"),
            models=models,
            geo_settings=GeoSettings(photos_dir=tmp_path / "fotos", **geo_overrides),
            geo_photos=make_photos(tmp_path / "fotos"),
        )
        return TestClient(app)

    return _make
