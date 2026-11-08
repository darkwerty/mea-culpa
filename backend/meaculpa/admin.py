"""Panel de administración (danielalonso.dev/admin): endpoints bajo /api/admin.

Acceso: ID token de Google verificado (firma, audiencia, caducidad y email_verified, como el resto de la API) y
correo en la lista blanca ADMIN_EMAILS (por defecto, solo admin@example.com). Sin token: 401; con el token de
cualquier otra cuenta: 403.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from .auth import User
from .costes_azure import AzureCosts
from .datos import DEMOS, DataStore
from .db import DatabaseUnavailable
from .estadisticas import WebStats
from .llm.base import LLMProvider
from .models import MODEL_CATALOG

ADMIN_PREFIX = "/api/admin"

QUOTA_LABELS = {
    "partidas": "Mea culpa: partidas nuevas por persona y día",
    "jugadas": "Mea culpa: jugadas por persona y día",
    "jugadas_llm": "Mea culpa: jugadas contra la IA por persona y día",
    "analisis": "Mea culpa: análisis por persona y día",
    "pistas": "Mea culpa: pistas por persona y día",
    "llm_global": "Mea culpa: llamadas a la IA al día (todas las personas)",
    "geo_partidas": "Fotos: partidas nuevas por persona y día",
    "geo_ia": "Fotos: fotos analizadas por la IA por persona y día",
    "geo_global": "Fotos: llamadas a la IA al día (todas las personas)",
    "geo_sv_partidas": "Dónde está: partidas con Street View por persona y día",
    "geo_sv_global": "Dónde está: partidas con Street View al día, todas las personas (tope de capturas)",
    "geo_mapas": "Dónde está: cargas de mapa y panorámica de Google para partidas nuevas al día (Google corta en 160)",
    "geo_panoramas": "Dónde está: panorámicas de Street View al día, todas las personas (gratis hasta unas 161)",
    "geo_recargas": "Dónde está: recargas de una partida a medias por persona y día",
}


class Toggle(BaseModel):
    activo: bool


class QuotaIn(BaseModel):
    tipo: str = Field(..., max_length=40)
    valor: int | None = Field(None, ge=0, le=1_000_000)  # None = volver al valor por defecto


class BlockIn(BaseModel):
    bloqueado: bool


class ReadIn(BaseModel):
    leido: bool = True


def create_admin_router(
    *,
    authenticate: Callable[[Request], User],
    admin_emails: tuple[str, ...],
    data: DataStore,
    costs: AzureCosts,
    models: dict[str, LLMProvider],
    quota_defaults: dict[str, int],
    web_stats: WebStats | None = None,
) -> APIRouter:
    allowed = {e.strip().lower() for e in admin_emails if e.strip()}

    def admin_user(request: Request) -> User:
        user = authenticate(request)  # 401 si no hay token válido (incluye email_verified)
        if not user.email or user.email.lower() not in allowed:
            raise HTTPException(403, "No tienes acceso al panel")
        data.touch_user(user.owner, user.email, user.name)
        return user

    router = APIRouter(prefix=ADMIN_PREFIX, dependencies=[Depends(admin_user)])

    @contextmanager
    def database() -> Iterator[None]:
        try:
            yield
        except DatabaseUnavailable as exc:
            raise HTTPException(
                503, "La base de datos está despertando o en pausa. Vuelve a intentarlo en un minuto."
            ) from exc

    def _model_rows() -> list[dict[str, Any]]:
        rows = [
            {
                "id": m.id,
                "etiqueta": m.etiqueta,
                "descripcion": m.descripcion,
                "desplegado": m.id in models,
                "activo": data.model_enabled(m.id),
            }
            for m in MODEL_CATALOG
        ]
        known = {m.id for m in MODEL_CATALOG}
        rows += [
            {
                "id": mid,
                "etiqueta": getattr(p, "name", mid),
                "descripcion": "Fuera del catálogo",
                "desplegado": True,
                "activo": data.model_enabled(mid),
            }
            for mid, p in models.items()
            if mid not in known
        ]
        return rows

    @router.get("/yo")
    def me(user: User = Depends(admin_user)) -> dict[str, Any]:
        return {"email": user.email, "nombre": user.name}

    @router.get("/resumen")
    def summary() -> dict[str, Any]:
        with database():
            return data.summary()

    @router.get("/usuarios")
    def users(limite: int = Query(200, ge=1, le=1000)) -> dict[str, Any]:
        with database():
            return {"usuarios": data.users(limite)}

    @router.post("/usuarios/{owner}/bloqueo")
    def block(owner: str, body: BlockIn) -> dict[str, Any]:
        if len(owner) != 64 or not owner.isalnum():
            raise HTTPException(422, "Identificador de usuario no válido")
        with database():
            email = data.user_email(owner)
            if body.bloqueado and email and email.lower() in allowed:
                raise HTTPException(409, "No puedes bloquear una cuenta de administración")
            if not data.set_blocked(owner, body.bloqueado):
                raise HTTPException(404, "Usuario no encontrado")
        return {"owner": owner, "bloqueado": body.bloqueado}

    @router.get("/uso")
    def usage(dias: int = Query(30, ge=1, le=365)) -> dict[str, Any]:
        with database():
            return {"por_dia": data.usage(dias), "demos": DEMOS}

    @router.get("/partidas")
    def games(limite: int = Query(50, ge=1, le=200)) -> dict[str, Any]:
        with database():
            return {"partidas": data.recent_games(limite)}

    @router.get("/partidas-por-modelo")
    def games_by_model(dias: int = Query(30, ge=1, le=365)) -> dict[str, Any]:
        with database():
            return {"partidas": data.games_by_model(dias), "demos": DEMOS}

    @router.get("/web")
    def web(dias: int = Query(30, ge=1, le=365)) -> dict[str, Any]:
        if web_stats is None:
            raise HTTPException(404, "Sin estadísticas de la web")
        with database():
            return web_stats.report(dias)

    @router.get("/gasto-ia")
    def ai_spend(dias: int = Query(30, ge=1, le=365)) -> dict[str, Any]:
        with database():
            return data.ai_spend(dias)

    @router.get("/gasto-azure")
    def azure_spend() -> dict[str, Any]:
        return costs.month()

    @router.get("/mensajes")
    def messages() -> dict[str, Any]:
        with database():
            return {"mensajes": data.messages()}

    @router.post("/mensajes/{msg_id}/leido")
    def mark_read(msg_id: str, body: ReadIn) -> dict[str, Any]:
        with database():
            if not data.set_read(msg_id[:32], body.leido):
                raise HTTPException(404, "Mensaje no encontrado")
        return {"id": msg_id, "leido": body.leido}

    @router.delete("/mensajes/{msg_id}")
    def delete_message(msg_id: str) -> dict[str, Any]:
        with database():
            if not data.delete_message(msg_id[:32]):
                raise HTTPException(404, "Mensaje no encontrado")
        return {"id": msg_id, "borrado": True}

    @router.get("/controles")
    def controls() -> dict[str, Any]:
        cfg = data.config()
        return {
            "demos": [{"id": d, "nombre": name, "activa": data.demo_enabled(d)} for d, name in DEMOS.items()],
            "modelos": _model_rows(),
            "cuotas": [
                {
                    "tipo": kind,
                    "etiqueta": QUOTA_LABELS.get(kind, kind),
                    "valor": data.quota(kind, default),
                    "por_defecto": default,
                    "cambiada": f"cuota.{kind}" in cfg,
                }
                for kind, default in quota_defaults.items()
            ],
        }

    @router.post("/controles/demos/{demo}")
    def toggle_demo(demo: Literal["meaculpa", "geo"], body: Toggle) -> dict[str, Any]:
        with database():
            data.set_config(f"demo.{demo}", "1" if body.activo else "0")
        return {"demo": demo, "activa": body.activo}

    @router.post("/controles/modelos/{model_id}")
    def toggle_model(model_id: str, body: Toggle) -> dict[str, Any]:
        if model_id not in {m.id for m in MODEL_CATALOG} | set(models):
            raise HTTPException(404, "Modelo desconocido")
        with database():
            data.set_config(f"modelo.{model_id}", "1" if body.activo else "0")
        return {"modelo": model_id, "activo": body.activo}

    @router.post("/controles/cuotas")
    def set_quota(body: QuotaIn) -> dict[str, Any]:
        if body.tipo not in quota_defaults:
            raise HTTPException(404, "Cuota desconocida")
        with database():
            if body.valor is None:
                data.delete_config(f"cuota.{body.tipo}")
            else:
                data.set_config(f"cuota.{body.tipo}", str(body.valor))
        return {"tipo": body.tipo, "valor": data.quota(body.tipo, quota_defaults[body.tipo])}

    return router
