"""Formulario de contacto público de danielalonso.dev/contacto: POST /api/contacto.

- Validación de longitud y formato; solo se guarda nombre, correo, mensaje y fecha (ni IP ni navegador).
- Campo trampa `web`: invisible para las personas; si llega relleno, se responde como si nada y no se guarda.
- Límites por IP y día y total diario (la IP solo se usa como hash del día en un contador que se borra al día
  siguiente).
- Los mensajes se borran a los 12 meses (datos.MESSAGE_RETENTION_S).
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from .config import Settings
from .datos import DataStore
from .db import DatabaseUnavailable

EMAIL_RE = re.compile(r"^[^@\s<>\"']{1,64}@[^@\s<>\"']+\.[^@\s<>\"']{2,}$")


def _clean(text: str, multiline: bool) -> str:
    """Quita caracteres de control (salvo saltos de línea en el mensaje) y espacios sobrantes."""
    keep = {"\n"} if multiline else set()
    out = "".join(c for c in text if c in keep or unicodedata.category(c)[0] != "C")
    return out.replace("\r", "").strip()


class ContactIn(BaseModel):
    nombre: str = Field(..., max_length=100)
    email: str = Field(..., max_length=320)
    mensaje: str = Field(..., max_length=4000)
    web: str = Field("", max_length=300)  # campo trampa

    @field_validator("nombre")
    @classmethod
    def _name(cls, v: str) -> str:
        v = _clean(v, multiline=False)
        if not v:
            raise ValueError("Escribe tu nombre")
        return v

    @field_validator("email")
    @classmethod
    def _email(cls, v: str) -> str:
        v = _clean(v, multiline=False).lower()
        if not EMAIL_RE.match(v):
            raise ValueError("Ese correo no parece válido")
        return v

    @field_validator("mensaje")
    @classmethod
    def _message(cls, v: str) -> str:
        v = _clean(v, multiline=True)
        if len(v) < 10:
            raise ValueError("El mensaje es demasiado corto")
        return v


def client_ip(request: Request) -> str:
    """IP del visitante tras la SWA y la entrada de Container Apps (solo para el límite diario)."""
    for header in ("x-azure-clientip", "x-client-ip"):
        value = request.headers.get(header, "").strip()
        if value:
            return value[:64]
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded.strip():
        return forwarded.split(",")[0].strip()[:64]
    return request.client.host if request.client else "desconocida"


def create_contact_router(*, data: DataStore, settings: Settings) -> APIRouter:
    router = APIRouter()

    @router.post("/api/contacto")
    def contact(body: ContactIn, request: Request) -> dict[str, Any]:
        if body.web.strip():
            return {"ok": True}  # bot: misma respuesta, nada guardado
        try:
            if not data.contact_slot(client_ip(request), settings.contact_per_ip_per_day, settings.contact_per_day):
                raise HTTPException(429, "Has enviado demasiados mensajes por hoy. Escríbeme por correo si es urgente.")
            data.add_message(body.nombre, body.email, body.mensaje)
        except DatabaseUnavailable as exc:
            raise HTTPException(
                503, "No se ha podido enviar ahora mismo. Prueba en un minuto o escríbeme a hola@danielalonso.dev."
            ) from exc
        return {"ok": True}

    return router
