"""Aviso por correo de cada mensaje del formulario de contacto (además de guardarlo para /admin).

Azure Communication Services Email con la identidad administrada de la API (sin claves). El correo va a
CONTACT_MAIL_TO (hola@danielalonso.dev) con «Responder a» la persona que escribió, así que se contesta desde el
propio correo. Se envía después de responder al formulario: si falla, el mensaje ya está guardado y solo se
registra el error.
"""

from __future__ import annotations

import html
import logging
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

log = logging.getLogger(__name__)


def _last_sunday(year: int, month: int) -> datetime:
    d = datetime(year, month + 1, 1, tzinfo=UTC) - timedelta(days=1)
    return d - timedelta(days=(d.weekday() + 1) % 7)


def madrid_time(ts: float) -> datetime:
    """Hora peninsular sin depender de tzdata (la imagen y Windows pueden no traerla): regla de verano de la UE."""
    utc = datetime.fromtimestamp(ts, UTC)
    start = _last_sunday(utc.year, 3).replace(hour=1)
    end = _last_sunday(utc.year, 10).replace(hour=1)
    return utc + timedelta(hours=2 if start <= utc < end else 1)


class ContactNotifier(Protocol):
    def send(self, nombre: str, email: str, mensaje: str, ts: float) -> None: ...


def build_email(nombre: str, email: str, mensaje: str, ts: float, *, sender: str, to: str) -> dict[str, Any]:
    """El mensaje en el formato de la API de ACS Email (texto plano y HTML con todo escapado)."""
    when = madrid_time(ts).strftime("%d/%m/%Y %H:%M")
    text = (
        f"{nombre} <{email}> te ha escrito desde el formulario de danielalonso.dev el {when}:\n\n"
        f"{mensaje}\n\n"
        f"Responde a este correo para contestarle. También está en https://danielalonso.dev/admin"
    )
    body = html.escape(mensaje).replace("\n", "<br>")
    page = (
        '<div style="font-family:Segoe UI,Arial,sans-serif;font-size:15px;line-height:1.5;color:#16181d">'
        f"<p><strong>{html.escape(nombre)}</strong> &lt;{html.escape(email)}&gt; te ha escrito desde el formulario de "
        f"danielalonso.dev el {when}:</p>"
        f'<p style="padding:12px 14px;border-left:3px solid #0f6cbd;background:#f4f6fa">{body}</p>'
        '<p style="color:#565c69;font-size:13px">Responde a este correo para contestarle. También está en '
        '<a href="https://danielalonso.dev/admin">danielalonso.dev/admin</a>.</p></div>'
    )
    return {
        "senderAddress": sender,
        "recipients": {"to": [{"address": to}]},
        "replyTo": [{"address": email, "displayName": nombre}],
        "content": {"subject": f"Mensaje de {nombre} en danielalonso.dev", "plainText": text, "html": page},
    }


class AcsNotifier:
    def __init__(self, endpoint: str, sender: str, to: str, credential: Any) -> None:
        from azure.communication.email import EmailClient

        self.client = EmailClient(endpoint, credential)
        self.sender = sender
        self.to = to

    def send(self, nombre: str, email: str, mensaje: str, ts: float) -> None:
        message = build_email(nombre, email, mensaje, ts, sender=self.sender, to=self.to)
        poller = self.client.begin_send(message)
        result = poller.result(timeout=60)
        status = (result or {}).get("status")
        if status and status.lower() != "succeeded":
            log.warning("Aviso de contacto: ACS devolvió %s", status)


def notify_safely(notifier: ContactNotifier, nombre: str, email: str, mensaje: str, ts: float) -> None:
    """Tarea de fondo: nunca rompe nada, solo deja el error en el registro."""
    try:
        notifier.send(nombre, email, mensaje, ts)
    except Exception:  # noqa: BLE001 - el mensaje ya está guardado; el aviso es lo de menos
        log.exception("No se pudo enviar el aviso por correo del formulario de contacto")
