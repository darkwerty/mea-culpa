"""Formulario de contacto público de danielalonso.dev/contacto: POST /api/contacto.

- Validación de longitud y formato; solo se guarda nombre, correo, mensaje y fecha (ni IP ni navegador).
- Campo trampa `web`: invisible para las personas; si llega relleno, se responde como si nada y no se guarda.
- Límites por IP y día y total diario (la IP solo se usa como hash del día en un contador que se borra al día
  siguiente).
- Los mensajes se borran a los 12 meses (datos.MESSAGE_RETENTION_S).
- Si hay `notifier`, cada mensaje guardado se avisa además por correo, después de responder (aviso.py).
- Responde en cuanto el mensaje está a salvo en Blob Storage (ContactInbox): no espera a la base de datos, que
  puede estar en pausa. Pasa a la base de datos justo después de responder.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
import unicodedata
import uuid
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from .aviso import ContactNotifier, notify_safely
from .config import Settings
from .datos import DataStore, day_of
from .db import DatabaseUnavailable
from .storage import ObjectStore

log = logging.getLogger(__name__)

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


# ---------------------------------------------------------------- buzón rápido
PENDING_PREFIX = "contacto/pendientes"
LIMITS_PREFIX = "contacto/limites"


class ContactInbox:
    """Buzón en Blob Storage para que el formulario responda al momento.

    La base de datos (Azure SQL serverless gratuita) se pausa sin uso y despertarla tarda hasta un minuto: el
    formulario no la espera. El límite diario se cuenta en Blob (escrituras condicionadas por ETag, como las
    cuotas), el mensaje se guarda en Blob y se responde. Después de responder se avisa por correo y el mensaje
    pasa a la base de datos; si está dormida, se queda en Blob y pasa la próxima vez (otro envío o /admin).
    """

    MAX_RETRIES = 6

    def __init__(self, objects: ObjectStore, data: DataStore, clock: Callable[[], float] = time.time) -> None:
        self.objects = objects
        self.data = data
        self.clock = clock
        self._flush_lock = threading.Lock()

    def slot(self, ip: str, per_ip: int, per_day: int) -> bool:
        """Cuenta un envío para esta IP y para el día. False si se pasa. La IP solo va como hash con el día."""
        today = day_of(self.clock())
        key = f"{LIMITS_PREFIX}/{today}"
        ip_key = hashlib.sha256(f"contacto:{today}:{ip}".encode()).hexdigest()[:32]
        for _ in range(self.MAX_RETRIES):
            found = self.objects.read(key)
            counts, etag = (json.loads(found[0]), found[1]) if found else ({}, None)
            if int(counts.get(ip_key, 0)) >= per_ip or int(counts.get("total", 0)) >= per_day:
                return False
            counts[ip_key] = int(counts.get(ip_key, 0)) + 1
            counts["total"] = int(counts.get("total", 0)) + 1
            if self.objects.write(key, json.dumps(counts).encode(), etag=etag) is not None:
                if found is None:  # primer envío del día: fuera los contadores de días anteriores
                    for old, _mtime in self.objects.list(LIMITS_PREFIX):
                        if old != key:
                            self.objects.delete(old)
                return True
        raise HTTPException(503, "Hay muchos envíos a la vez. Prueba otra vez en unos segundos.")

    def stash(self, nombre: str, email: str, mensaje: str) -> dict[str, Any]:
        """Guarda el mensaje en Blob (milisegundos) y lo devuelve con su id y su fecha."""
        msg = {"id": uuid.uuid4().hex, "ts": self.clock(), "nombre": nombre, "email": email, "mensaje": mensaje}
        key = f"{PENDING_PREFIX}/{int(msg['ts'])}-{msg['id']}.json"
        if self.objects.write(key, json.dumps(msg, ensure_ascii=False).encode()) is None:
            raise RuntimeError("no se pudo guardar el mensaje pendiente")
        return msg

    def flush(self) -> int:
        """Pasa a la base de datos los mensajes pendientes. Para en cuanto no responde; nunca falla."""
        if not self._flush_lock.acquire(blocking=False):
            return 0  # ya lo está haciendo otra petición
        moved = 0
        try:
            for key, _mtime in sorted(self.objects.list(PENDING_PREFIX)):
                found = self.objects.read(key)
                if found is None:
                    continue
                m = json.loads(found[0])
                try:
                    self.data.add_message(m["nombre"], m["email"], m["mensaje"], msg_id=m["id"], ts=m["ts"])
                except DatabaseUnavailable:
                    log.info("Base de datos dormida: %s se queda pendiente", key)
                    break
                self.objects.delete(key)
                moved += 1
        except Exception:  # noqa: BLE001 - tarea de fondo: el mensaje sigue en Blob
            log.exception("No se pudieron pasar los mensajes pendientes a la base de datos")
        finally:
            self._flush_lock.release()
        return moved


def create_contact_router(
    *,
    data: DataStore,
    settings: Settings,
    notifier: ContactNotifier | None = None,
    inbox: ContactInbox | None = None,
) -> APIRouter:
    router = APIRouter()

    def after_response(nombre: str, email: str, mensaje: str, ts: float) -> None:
        # Primero el correo (es lo que avisa a Daniel), luego la base de datos, que puede tardar en despertar
        if notifier is not None:
            notify_safely(notifier, nombre, email, mensaje, ts)
        if inbox is not None:
            inbox.flush()

    @router.post("/api/contacto")
    def contact(body: ContactIn, request: Request, background: BackgroundTasks) -> dict[str, Any]:
        if body.web.strip():
            return {"ok": True}  # bot: misma respuesta, nada guardado
        too_many = HTTPException(429, "Has enviado demasiados mensajes por hoy. Escríbeme por correo si es urgente.")
        if inbox is not None:
            try:
                if not inbox.slot(client_ip(request), settings.contact_per_ip_per_day, settings.contact_per_day):
                    raise too_many
                msg = inbox.stash(body.nombre, body.email, body.mensaje)
            except HTTPException:
                raise
            except Exception:  # noqa: BLE001 - sin Blob, se guarda directamente en la base de datos
                log.exception("Buzón rápido no disponible: se guarda en la base de datos")
            else:
                background.add_task(after_response, body.nombre, body.email, body.mensaje, msg["ts"])
                return {"ok": True}
        try:
            if not data.contact_slot(client_ip(request), settings.contact_per_ip_per_day, settings.contact_per_day):
                raise too_many
            data.add_message(body.nombre, body.email, body.mensaje)
        except DatabaseUnavailable as exc:
            raise HTTPException(
                503, "No se ha podido enviar ahora mismo. Prueba en un minuto o escríbeme a hola@danielalonso.dev."
            ) from exc
        if notifier is not None:
            background.add_task(notify_safely, notifier, body.nombre, body.email, body.mensaje, time.time())
        return {"ok": True}

    return router
