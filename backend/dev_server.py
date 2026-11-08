"""Servidor SOLO para desarrollo local, sin cuenta de Google.

Acepta cualquier petición como un único usuario local. No forma parte del paquete `meaculpa` y el
Dockerfile no lo copia, así que no existe en la imagen desplegada: allí la API siempre exige un ID token
de Google válido.

    .venv/Scripts/python -m uvicorn dev_server:app --app-dir backend --port 8000
"""

from __future__ import annotations

import dataclasses
import time

from meaculpa.config import get_settings
from meaculpa.main import create_app

DEV_CLIENT_ID = "desarrollo-local"


def _local_user(_token: str) -> dict:
    return {
        "aud": DEV_CLIENT_ID,
        "iss": "https://accounts.google.com",
        "exp": time.time() + 3600,
        "sub": "usuario-local",
        "email_verified": True,
        "given_name": "Local",
    }


app = create_app(
    settings=dataclasses.replace(get_settings(), google_client_id=DEV_CLIENT_ID),
    token_verifier=_local_user,
)
