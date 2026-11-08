"""Login con Google Identity Services: el frontend manda el ID token en `Authorization: Bearer`.

Se verifica la firma (claves públicas de Google), `aud` == GOOGLE_CLIENT_ID, `iss`, `exp` y que el
correo esté verificado. El usuario se identifica por un hash del `sub`: el `sub` en claro no llega al
almacenamiento ni a los logs. El correo solo se guarda en la tabla de usuarios de la base de datos (para el panel
/admin y para atender peticiones de privacidad) y decide quién entra en /admin.

No hay ningún modo sin autenticación configurable por variable de entorno. Para desarrollo local sin
cuenta de Google existe `backend/dev_server.py`, que no se copia a la imagen Docker.
"""

from __future__ import annotations

import hashlib
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from fastapi import HTTPException, Request

GOOGLE_ISSUERS = {"accounts.google.com", "https://accounts.google.com"}
CERTS_CACHE_SECONDS = 3600


@dataclass(frozen=True)
class User:
    owner: str  # sha256 del sub: clave de almacenamiento y de cuotas
    name: str | None = None
    email: str | None = field(default=None, repr=False)  # verificado por Google (email_verified)


TokenVerifier = Callable[[str], dict[str, Any]]


def owner_id(sub: str) -> str:
    return hashlib.sha256(f"google:{sub}".encode()).hexdigest()


class _CachingRequest:
    """Transporte de google-auth que guarda la respuesta de los certificados de Google una hora."""

    def __init__(self) -> None:
        from google.auth.transport.requests import Request as GoogleRequest

        self._inner = GoogleRequest()
        self._cache: dict[str, tuple[float, Any]] = {}
        self._lock = threading.Lock()

    def __call__(self, url: str, method: str = "GET", **kwargs: Any) -> Any:
        if method != "GET":
            return self._inner(url, method=method, **kwargs)
        with self._lock:
            hit = self._cache.get(url)
            if hit and time.monotonic() - hit[0] < CERTS_CACHE_SECONDS:
                return hit[1]
        response = self._inner(url, method=method, timeout=10, **kwargs)
        if response.status == 200:
            with self._lock:
                self._cache[url] = (time.monotonic(), response)
        return response


def google_verifier(client_id: str, request: Any = None) -> TokenVerifier:
    """Verificador real. `request` (transporte de google-auth) solo se sustituye en los tests."""
    from google.oauth2 import id_token

    if not client_id:
        raise ValueError("client_id vacío: verify_oauth2_token no comprobaría la audiencia")
    request = request or _CachingRequest()

    def verify(token: str) -> dict[str, Any]:
        # Firma, `aud`, `exp` e `iss`; validate_claims repite aud/iss/exp y añade email_verified.
        return id_token.verify_oauth2_token(token, request, audience=client_id, clock_skew_in_seconds=10)

    return verify


def validate_claims(claims: dict[str, Any], client_id: str) -> User:
    """Comprobaciones explícitas además de las de la librería (defensa en profundidad)."""
    if claims.get("aud") != client_id:
        raise ValueError("aud no coincide")
    if claims.get("iss") not in GOOGLE_ISSUERS:
        raise ValueError("iss no es Google")
    if float(claims.get("exp", 0)) < time.time() - 10:
        raise ValueError("token caducado")
    if claims.get("email_verified") is not True:
        raise ValueError("correo no verificado")
    sub = claims.get("sub")
    if not isinstance(sub, str) or not sub:
        raise ValueError("falta sub")
    name = claims.get("given_name") or claims.get("name")
    email = claims.get("email")
    email = email.strip().lower()[:320] if isinstance(email, str) and email.strip() else None
    return User(owner=owner_id(sub), name=str(name)[:60] if name else None, email=email)


def make_user_dependency(verifier: TokenVerifier, client_id: str) -> Callable[[Request], User]:
    def current_user(request: Request) -> User:
        header = request.headers.get("authorization", "")
        scheme, _, token = header.partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            raise HTTPException(401, "Inicia sesión con Google", headers={"WWW-Authenticate": "Bearer"})
        if len(token) > 4096:
            raise HTTPException(401, "Token no válido", headers={"WWW-Authenticate": "Bearer"})
        try:
            claims = verifier(token.strip())
            return validate_claims(claims, client_id)
        except Exception as exc:  # noqa: BLE001 - cualquier fallo de verificación es un 401
            raise HTTPException(401, "Sesión no válida o caducada", headers={"WWW-Authenticate": "Bearer"}) from exc

    return current_user
