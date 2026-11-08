"""Estadísticas de visitas de danielalonso.dev, sin cookies: POST /api/stats/visita.

La web lo llama con `navigator.sendBeacon` en cada cambio de ruta. Se guarda ruta, dominio de procedencia (solo
si es externo), país (cabecera de país si la hay; si no, el de la zona horaria del navegador), tipo de dispositivo
(por el user agent) y fecha. Nada de IP ni identificadores persistentes: para contar visitantes únicos del día se
guarda un hash de IP y navegador con una sal aleatoria que solo vive en memoria y cambia cada día. (Si la API se
reinicia a media jornada, la sal cambia y alguien puede contar dos veces ese día: es un recuento aproximado.)

Coste y base de datos en pausa: una visita NO despierta Azure SQL. Las visitas se acumulan en memoria, se pasan a
Blob (`stats/pendiente/`) cada minuto y al apagarse la API, y se vuelcan a SQL solo cuando la base de datos ya está
despierta por otra cosa (una partida, el panel /admin) o, como mucho, una vez al día. Si algo falla, se pierde la
visita y nada más: la web nunca espera ni falla por esto.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
import threading
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Request, Response

from .contacto import client_ip
from .db import Database, DatabaseUnavailable
from .storage import ObjectStore

log = logging.getLogger("meaculpa.estadisticas")

PENDING_PREFIX = "stats/pendiente"
RETENTION_S = 365 * 86400  # 12 meses
OWN_HOSTS = ("danielalonso.dev", "azurestaticapps.net", "localhost", "127.0.0.1")
ROUTE_RE = re.compile(r"^/[A-Za-z0-9/_\-.]{0,199}$")
BOT_RE = re.compile(r"bot|crawl|spider|slurp|headless|lighthouse|preview|monitor|curl|wget|python|httpx", re.I)
TABLET_RE = re.compile(r"ipad|tablet|kindle|silk|playbook|android(?!.*mobile)", re.I)
MOBILE_RE = re.compile(r"mobi|iphone|ipod|android|windows phone", re.I)
COUNTRY_HEADERS = ("cf-ipcountry", "x-azure-clientcountry", "x-country-code", "x-client-geo-country")
_ZONES: dict[str, str] | None = None


def _zones() -> dict[str, str]:
    global _ZONES
    if _ZONES is None:
        path = Path(__file__).parent / "datos_estaticos" / "zonas_pais.json"  # de tzdata (zone.tab)
        _ZONES = json.loads(path.read_text(encoding="utf-8"))
    return _ZONES


def device(user_agent: str) -> str | None:
    """movil | tableta | ordenador, o None si es un bot (no se cuenta)."""
    if not user_agent or BOT_RE.search(user_agent):
        return None
    if TABLET_RE.search(user_agent):
        return "tableta"
    if MOBILE_RE.search(user_agent):
        return "movil"
    return "ordenador"


def referer_domain(ref: str | None) -> str | None:
    if not ref or not isinstance(ref, str):
        return None
    try:
        host = (urlsplit(ref.strip()[:500]).hostname or "").lower()
    except ValueError:
        return None
    host = host.removeprefix("www.")
    if not host or not re.fullmatch(r"[a-z0-9.\-]{1,120}", host) or any(host.endswith(h) for h in OWN_HOSTS):
        return None
    return host


def country(request: Request, tz: str | None) -> str | None:
    for header in COUNTRY_HEADERS:
        value = request.headers.get(header, "").strip().upper()
        if re.fullmatch(r"[A-Z]{2}", value) and value not in {"XX", "T1"}:
            return value
    if tz and isinstance(tz, str) and len(tz) <= 60:
        return _zones().get(tz)
    return None


def clean_route(route: Any) -> str | None:
    if not isinstance(route, str):
        return None
    route = route.split("?")[0].split("#")[0]
    if len(route) > 1:
        route = route.rstrip("/")
    return route if ROUTE_RE.match(route) else None


class WebStats:
    PERSIST_EVERY = 60.0  # s: de memoria a Blob
    FORCE_FLUSH_AFTER = 86400.0  # s: si la base de datos no se ha despertado por otra cosa, una vez al día
    PER_VISITOR_WINDOW = 600.0
    PER_VISITOR_LIMIT = 60  # visitas por visitante en 10 minutos
    PER_DAY_LIMIT = 20_000  # visitas al día en total (tope contra abusos)

    def __init__(self, db: Database, objects: ObjectStore, clock=time.time) -> None:
        self.db = db
        self.objects = objects
        self.clock = clock
        self._lock = threading.Lock()
        self._flush_lock = threading.Lock()
        self._buffer: list[dict[str, Any]] = []
        self._buffer_since = 0.0
        self._salt_day = ""
        self._salt = b""
        self._hits: dict[str, tuple[float, int]] = {}
        self._day_count = 0
        self._last_flush = time.monotonic()

    def _visitor(self, day: str, ip: str, user_agent: str) -> str:
        if day != self._salt_day:  # sal nueva cada día, solo en memoria
            self._salt_day, self._salt = day, secrets.token_bytes(16)
            self._hits.clear()
            self._day_count = 0
        return hashlib.sha256(self._salt + f"{ip}|{user_agent}".encode()).hexdigest()[:16]

    def hit(self, request: Request, body: dict[str, Any]) -> bool:
        """Anota una visita. False si no se cuenta (bot, ruta no válida o por encima de los límites)."""
        route = clean_route(body.get("ruta"))
        ua = request.headers.get("user-agent", "")[:400]
        kind = device(ua)
        if route is None or kind is None or route.startswith(("/admin", "/api")):
            return False
        now = self.clock()
        day = time.strftime("%Y-%m-%d", time.gmtime(now))
        with self._lock:
            visitor = self._visitor(day, client_ip(request), ua)
            start, count = self._hits.get(visitor, (now, 0))
            if now - start > self.PER_VISITOR_WINDOW:
                start, count = now, 0
            if count >= self.PER_VISITOR_LIMIT or self._day_count >= self.PER_DAY_LIMIT:
                return False
            self._hits[visitor] = (start, count + 1)
            self._day_count += 1
            if len(self._hits) > 50_000:  # memoria acotada
                self._hits.clear()
            if not self._buffer:
                self._buffer_since = time.monotonic()
            self._buffer.append(
                {
                    "id": uuid.uuid4().hex,
                    "ts": now,
                    "dia": day,
                    "ruta": route,
                    "referer": referer_domain(body.get("ref")),
                    "pais": country(request, body.get("tz")),
                    "dispositivo": kind,
                    "visitante": visitor,
                }
            )
            due = time.monotonic() - self._buffer_since >= self.PERSIST_EVERY or len(self._buffer) >= 200
        if due:
            threading.Thread(target=self.persist_and_maybe_flush, daemon=True).start()
        return True

    # ---------------------------------------------------------------- memoria -> Blob -> SQL
    def persist(self) -> None:
        with self._lock:
            rows, self._buffer = self._buffer, []
        if not rows:
            return
        key = f"{PENDING_PREFIX}/{int(self.clock() * 1000)}-{uuid.uuid4().hex[:8]}.json"
        try:
            self.objects.write(key, json.dumps(rows).encode())
        except Exception as exc:  # noqa: BLE001 - se pierden estas visitas, nada más
            log.warning("No se pudieron guardar %d visitas en Blob: %s", len(rows), exc)

    def persist_and_maybe_flush(self) -> None:
        self.persist()
        overdue = time.monotonic() - self._last_flush > self.FORCE_FLUSH_AFTER
        if self.db.dialect == "sqlite" or self.db.recently_awake() or overdue:
            self.flush()

    def flush(self, wait: bool = False) -> int:
        """Pasa a SQL las visitas pendientes en Blob. Idempotente (cada visita tiene su id). Devuelve cuántas."""
        if not self._flush_lock.acquire(blocking=wait, timeout=20 if wait else -1):
            return 0
        try:
            self._last_flush = time.monotonic()
            try:
                keys = [k for k, _ in self.objects.list(PENDING_PREFIX)]
            except Exception as exc:  # noqa: BLE001
                log.warning("No se pudo listar %s: %s", PENDING_PREFIX, exc)
                return 0
            rows: list[dict[str, Any]] = []
            for key in keys[:500]:
                found = self.objects.read(key)
                if found:
                    rows.extend(json.loads(found[0]))
            if not rows:
                return 0
            cutoff = self.clock() - RETENTION_S

            def job(cur: Any) -> None:
                cur.execute("DELETE FROM visitas WHERE ts < ?", (cutoff,))  # conservación: 12 meses
                for r in rows:
                    cur.execute(
                        "INSERT INTO visitas (id, ts, dia, ruta, referer, pais, dispositivo, visitante) "
                        "SELECT ?, ?, ?, ?, ?, ?, ?, ? WHERE NOT EXISTS (SELECT 1 FROM visitas WHERE id = ?)",
                        (
                            r["id"],
                            r["ts"],
                            r["dia"],
                            r["ruta"],
                            r["referer"],
                            r["pais"],
                            r["dispositivo"],
                            r["visitante"],
                            r["id"],
                        ),
                    )

            try:
                self.db.run(job)
            except DatabaseUnavailable:
                return 0
            for key in keys[:500]:
                try:
                    self.objects.delete(key)
                except Exception as exc:  # noqa: BLE001 - se reintentará; el id evita duplicados
                    log.warning("No se pudo borrar %s: %s", key, exc)
            return len(rows)
        finally:
            self._flush_lock.release()

    # ---------------------------------------------------------------- panel /admin
    def report(self, days: int = 30) -> dict[str, Any]:
        """Para /admin (la base de datos ya está despierta): vuelca lo pendiente y agrega."""
        self.persist()
        self.flush(wait=True)
        since = time.strftime("%Y-%m-%d", time.gmtime(self.clock() - (days - 1) * 86400))
        q = self.db.query
        per_day = q(
            "SELECT dia, COUNT(*) AS visitas, COUNT(DISTINCT visitante) AS visitantes FROM visitas "
            "WHERE dia >= ? GROUP BY dia ORDER BY dia DESC",
            (since,),
        )

        def grouped(column: str, n: int = 20) -> list[dict[str, Any]]:
            top, lim = self.db.top(n)
            return [
                {"valor": r["valor"], "visitas": int(r["n"])}
                for r in q(
                    f"SELECT {top} {column} AS valor, COUNT(*) AS n FROM visitas WHERE dia >= ? "
                    f"AND {column} IS NOT NULL GROUP BY {column} ORDER BY n DESC {lim}",
                    (since,),
                )
            ]

        totals = q(
            "SELECT COUNT(*) AS visitas, SUM(CASE WHEN referer IS NULL THEN 1 ELSE 0 END) AS directas "
            "FROM visitas WHERE dia >= ?",
            (since,),
        )[0]
        return {
            "desde": since,
            "total": int(totals["visitas"] or 0),
            "sin_referer": int(totals["directas"] or 0),
            "por_dia": [
                {"dia": r["dia"], "visitas": int(r["visitas"]), "visitantes": int(r["visitantes"])} for r in per_day
            ],
            "paginas": grouped("ruta"),
            "referers": grouped("referer"),
            "paises": grouped("pais", 30),
            "dispositivos": grouped("dispositivo", 5),
        }


def create_stats_router(stats: WebStats) -> APIRouter:
    router = APIRouter()

    @router.post("/api/stats/visita", status_code=204)
    async def visit(request: Request) -> Response:
        """Siempre 204 (también si no se cuenta): no da pistas a quien quiera abusar ni hace esperar a la web."""
        raw = await request.body()
        if len(raw) <= 2048:
            try:
                body = json.loads(raw or b"{}")
                if isinstance(body, dict):
                    stats.hit(request, body)
            except (ValueError, UnicodeDecodeError):
                pass
        return Response(status_code=204)

    return router
