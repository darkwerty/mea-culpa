"""Partidas de Chess.com de quien ha entrado (rutas bajo /api/meaculpa/chesscom). Solo las ve esa persona.

- GET    /chesscom                          usuario conectado, cuándo se trajeron y la lista (sin jugadas).
- POST   /chesscom                          conecta un usuario de Chess.com (o trae las nuevas del ya conectado).
- DELETE /chesscom                          desconecta y borra todo lo traído (partidas y análisis).
- GET    /chesscom/partidas/{id}            una partida para repasarla en el tablero.
- POST   /chesscom/partidas/{id}/analysis   diagnóstico (lo hace main.py con las mismas cuotas que el resto).
- GET    /chesscom/partidas/{id}/analysis   el diagnóstico ya hecho.

Dónde se guardan: en el mismo Blob que las partidas de Mea culpa, pero aparte y solo con el hash de la cuenta de
Google: `chesscom/{owner}.json` (usuario de Chess.com, fecha y, de cada partida, jugadas, resultado, fecha, ritmo, Elo
y nombre del rival) y `chesscom-analisis/{owner}/{id}.json` (el diagnóstico de las que se analizan). No pasan por la
base de datos SQL ni por los rankings: nada de esto sale en ninguna página pública ni en /admin.

Topes: partidas de los últimos `MONTHS` meses y como mucho `MAX_GAMES` (las más recientes); una importación cada
`COOLDOWN_S` segundos por persona y la cuota diaria «importaciones» (configurable en /admin).
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .auth import User
from .chesscom import ChessComClient, ChessComError, ChessComNotFound, pick, valid_username
from .games import _HEX, GameRecord, game_state
from .quota import UsageLimiter
from .storage import ObjectStore

MONTHS = 3
MAX_GAMES = 100
COOLDOWN_S = 5 * 60


class ConnectRequest(BaseModel):
    usuario: str = Field(..., min_length=1, max_length=40)


def summary(entry: dict[str, Any]) -> dict[str, Any]:
    """Lo que necesita la lista de «Mis partidas» (sin las jugadas)."""
    return {k: v for k, v in entry.items() if k != "moves"} | {"jugadas": len(entry.get("moves") or [])}


def to_record(entry: dict[str, Any], owner: str, username: str | None) -> GameRecord:
    """La partida como GameRecord (terminada, de solo lectura) para el tablero y el análisis."""
    return GameRecord(
        id=entry["id"],
        created=float(entry.get("created") or 0),
        user_color=entry["user_color"],
        opponent={"type": "chesscom", "nombre": entry.get("rival") or "rival"},
        moves=list(entry.get("moves") or []),
        status="terminada",
        result=entry.get("result") or "*",
        resultado_usuario=entry.get("resultado_usuario"),
        motivo_fin=entry.get("motivo_fin"),
        owner=owner,
        origen="chesscom",
        externo={
            "usuario": username,
            "elo": entry.get("elo"),
            "rival_elo": entry.get("rival_elo"),
            "ritmo": entry.get("ritmo"),
            "control": entry.get("control"),
            "url": entry.get("url"),
        },
    )


class ImportStore:
    """Las partidas traídas de Chess.com de cada persona: un objeto con la lista y otro por cada análisis."""

    MAX_RETRIES = 8

    def __init__(self, objects: ObjectStore) -> None:
        self.objects = objects

    @staticmethod
    def _key(owner: str) -> str:
        if not _HEX.match(owner):
            raise ValueError("Identificador no válido")
        return f"chesscom/{owner}.json"

    @staticmethod
    def _analysis_key(owner: str, game_id: str) -> str:
        if not _HEX.match(owner) or not _HEX.match(game_id):
            raise ValueError("Identificador no válido")
        return f"chesscom-analisis/{owner}/{game_id}.json"

    def get(self, owner: str) -> dict[str, Any]:
        found = self.objects.read(self._key(owner))
        data = json.loads(found[0]) if found else {}
        return {
            "usuario": data.get("usuario"),
            "actualizado": data.get("actualizado"),
            "partidas": data.get("partidas") or [],
        }

    def update(self, owner: str, change: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
        """Lee, aplica `change` y guarda con control de versión (dos pestañas a la vez no se pisan)."""
        for _ in range(self.MAX_RETRIES):
            found = self.objects.read(self._key(owner))
            data, etag = (json.loads(found[0]), found[1]) if found else ({}, None)
            data.setdefault("partidas", [])
            change(data)
            raw = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            if self.objects.write(self._key(owner), raw, etag=etag) is not None:
                return data
        raise HTTPException(503, "Demasiadas peticiones a la vez; vuelve a intentarlo")

    def entry(self, owner: str, game_id: str) -> tuple[dict[str, Any], str | None] | None:
        if not _HEX.match(game_id or ""):
            return None
        data = self.get(owner)
        found = next((g for g in data["partidas"] if g.get("id") == game_id), None)
        return (found, data["usuario"]) if found else None

    def analysis(self, owner: str, game_id: str) -> dict[str, Any] | None:
        found = self.objects.read(self._analysis_key(owner, game_id))
        return json.loads(found[0]) if found else None

    def save_analysis(
        self, owner: str, game_id: str, payload: dict[str, Any], titular: str | None, patterns: list[str]
    ):
        self.objects.write(self._analysis_key(owner, game_id), json.dumps(payload, ensure_ascii=False).encode("utf-8"))

        def change(data: dict[str, Any]) -> None:
            for g in data["partidas"]:
                if g.get("id") == game_id:
                    g["analizada"] = True
                    g["titular"] = titular
                    g["patrones"] = patterns

        self.update(owner, change)

    def patterns(self, owner: str, exclude_id: str) -> list[list[str]]:
        """Patrones de las ya analizadas, para el «otra vez...» del diagnóstico."""
        return [
            list(g.get("patrones") or [])
            for g in self.get(owner)["partidas"]
            if g.get("id") != exclude_id and g.get("analizada")
        ]

    def delete_all(self, owner: str) -> int:
        """Borra la lista y todos los análisis. Devuelve cuántas partidas había."""
        n = len(self.get(owner)["partidas"])
        for key, _ in self.objects.list(f"chesscom-analisis/{owner}"):
            self.objects.delete(key)
        self.objects.delete(self._key(owner))
        return n

    def drop_analyses(self, owner: str, keep: set[str]) -> None:
        for key, _ in self.objects.list(f"chesscom-analisis/{owner}"):
            if key.rsplit("/", 1)[-1].removesuffix(".json") not in keep:
                self.objects.delete(key)


def status_payload(data: dict[str, Any], now: float) -> dict[str, Any]:
    games = sorted(data["partidas"], key=lambda g: g.get("created") or 0, reverse=True)
    last = data.get("actualizado")
    return {
        "usuario": data.get("usuario"),
        "actualizado": last,
        "siguiente": (last + COOLDOWN_S) if last and now < last + COOLDOWN_S else None,
        "meses": MONTHS,
        "maximo": MAX_GAMES,
        "partidas": [summary(g) for g in games],
    }


def _wait_text(seconds: float) -> str:
    mins = max(1, round(seconds / 60))
    return f"{mins} {'minuto' if mins == 1 else 'minutos'}"


def create_chesscom_router(
    *,
    current_user: Callable[..., User],
    imports: ImportStore,
    client: ChessComClient,
    limiter: UsageLimiter,
    record: Callable[..., None],
    clock: Callable[[], float] = time.time,
) -> APIRouter:
    router = APIRouter(prefix="/api/meaculpa/chesscom")

    @router.get("")
    def status(user: User = Depends(current_user)) -> dict[str, Any]:
        return status_payload(imports.get(user.owner), clock())

    @router.post("")
    def connect(req: ConnectRequest, user: User = Depends(current_user)) -> dict[str, Any]:
        name = valid_username(req.usuario)
        if not name:
            raise HTTPException(
                422, "Escribe tu usuario de Chess.com tal cual: de 3 a 25 letras, cifras, guiones o guiones bajos."
            )
        now = clock()
        current = imports.get(user.owner)
        last = current.get("actualizado")
        if last and now < last + COOLDOWN_S:
            raise HTTPException(
                429,
                f"Acabas de traer tus partidas. Puedes volver a hacerlo dentro de {_wait_text(last + COOLDOWN_S - now)}.",
            )
        limiter.consume(user.owner, "importaciones")
        try:
            raw, months = client.recent_games(name, now, MONTHS, MAX_GAMES)
        except ChessComNotFound as exc:
            # No se devuelve la cuota: así no sirve para probar nombres sin límite.
            raise HTTPException(404, str(exc)) from exc
        except ChessComError as exc:
            limiter.refund(user.owner, "importaciones")  # el fallo es de Chess.com, no de quien importa
            raise HTTPException(exc.status, str(exc)) from exc
        fresh, skipped = pick(raw, name, user.owner, MAX_GAMES)
        if not fresh:
            if skipped.variantes:
                msg = (
                    f"«{name}» no tiene partidas de ajedrez estándar en los últimos {MONTHS} meses (solo de variantes)."
                )
            else:
                msg = f"«{name}» no tiene partidas en Chess.com en los últimos {MONTHS} meses."
            raise HTTPException(404, msg)

        same_user = (current.get("usuario") or "").lower() == name.lower()
        kept_ids: set[str] = set()

        def change(data: dict[str, Any]) -> None:
            old = {g["id"]: g for g in data.get("partidas") or []} if same_user else {}
            merged = dict(old)
            for g in fresh:
                prev = old.get(g["id"])
                merged[g["id"]] = {
                    **g,
                    **{k: prev[k] for k in ("analizada", "titular", "patrones") if prev and k in prev},
                }
            games = sorted(merged.values(), key=lambda g: g.get("created") or 0, reverse=True)
            # Las más recientes, sin perder las que ya tienen diagnóstico (están acotadas por la cuota de análisis).
            top = games[:MAX_GAMES]
            top += [g for g in games[MAX_GAMES:] if g.get("analizada")]
            data["usuario"] = name
            data["actualizado"] = now
            data["partidas"] = top
            kept_ids.clear()
            kept_ids.update(g["id"] for g in top)

        before = {g["id"] for g in current["partidas"]} if same_user else set()
        data = imports.update(user.owner, change)
        if not same_user and current["partidas"]:
            imports.drop_analyses(user.owner, keep=set())  # eran de otra cuenta de Chess.com
        elif same_user:
            imports.drop_analyses(user.owner, keep=kept_ids)
        new = len({g["id"] for g in fresh} - before)
        record(user, "importacion_chesscom")
        payload = status_payload(data, now)
        variants = f" {skipped.variantes} de variantes no se traen." if skipped.variantes else ""
        if same_user:
            payload["mensaje"] = (
                f"{new} {'partida nueva' if new == 1 else 'partidas nuevas'} de {name}.{variants}"
                if new
                else f"No hay partidas nuevas de {name}.{variants}"
            )
        else:
            n = len(fresh)
            payload["mensaje"] = (
                f"{n} {'partida' if n == 1 else 'partidas'} de {name} de los últimos {MONTHS} meses.{variants}"
            )
        payload["nuevas"] = new
        payload["meses_leidos"] = [f"{y:04d}-{m:02d}" for y, m in months]
        return payload

    @router.delete("")
    def disconnect(user: User = Depends(current_user)) -> dict[str, Any]:
        n = imports.delete_all(user.owner)
        record(user, "importacion_borrada")
        return {"borradas": n}

    @router.get("/partidas/{game_id}")
    def get_game(game_id: str, user: User = Depends(current_user)) -> dict[str, Any]:
        found = imports.entry(user.owner, game_id)
        if found is None:
            raise HTTPException(404, "Partida no encontrada")
        entry, username = found
        state = game_state(to_record(entry, user.owner, username))
        return {**state, "has_diagnosis": bool(entry.get("analizada")), "procesando": False}

    @router.get("/partidas/{game_id}/analysis")
    def get_analysis(game_id: str, user: User = Depends(current_user)) -> dict[str, Any]:
        if imports.entry(user.owner, game_id) is None:
            raise HTTPException(404, "Partida no encontrada")
        found = imports.analysis(user.owner, game_id)
        if found is None:
            raise HTTPException(404, "Esta partida aún no tiene análisis")
        return found

    return router
