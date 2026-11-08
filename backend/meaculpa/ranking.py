"""Rankings públicos de las dos demos, con el nombre de Google de quien ha jugado (si lo acepta).

Dónde se guarda:
- Azure SQL (la base de datos de datos.py): `ranking_partidas` (una fila por partida terminada) y
  `ranking_jugadores` (nombre de pila de Google y si quiere salir con su nombre). Migración 002_ranking.sql.
- Blob (`ranking/<demo>.json`): una foto fija del ranking ya calculado, que se rehace cada vez que se escribe.

Las lecturas públicas solo leen esa foto fija (más una caché en memoria de 60 s): un visitante que mira el
ranking nunca despierta la base de datos serverless ni gasta su límite gratuito. Las escrituras van a la cola en
segundo plano de db.py, así que el final de una partida no espera a la base de datos aunque esté en pausa.

Qué se valida: el resultado lo calcula el servidor con su propia partida (puntos del juego de fotos, resultado de
python-chess o abandono). El cliente solo manda la casilla «Salir en el ranking con mi nombre». Cada partida
cuenta una vez (la clave es demo:id). Las partidas de ajedrez con ayuda (pistas o jugadas deshechas) no se
registran; si una ya registrada se reabre deshaciendo jugadas, su primer resultado se queda (si se borrara, bastaría
con deshacer después de perder para quitarse las derrotas). Quien desmarca la casilla deja de salir en las tablas de jugadores; sus
partidas siguen contando, sin nombre, en las estadísticas de cada modelo.

Migración a otro almacén: `export_rows()` devuelve todas las filas de las dos tablas como listas de dicts.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, ConfigDict

from .auth import User
from .db import Database
from .storage import ObjectStore

log = logging.getLogger("meaculpa.ranking")

DEMOS = ("geo", "geo-sv", "meaculpa")  # geo: partidas con fotos de Commons; geo-sv: con Street View
TOP_PLAYERS = 20
CACHE_SECONDS = 60.0
RESULTS = ("victoria", "tablas", "derrota")

_FORBIDDEN = str.maketrans({"·": " ", "—": " ", "–": " ", "<": "", ">": ""})
_SPACES = re.compile(r"\s+")


def clean_name(name: str | None) -> str | None:
    """Nombre visible: sin caracteres de control ni los que la web no muestra, como mucho 40 caracteres."""
    if not name:
        return None
    text = "".join(ch for ch in str(name) if ch.isprintable()).translate(_FORBIDDEN)
    text = _SPACES.sub(" ", text).strip()[:40].strip()
    return text or None


def _day(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(ts))


def chess_rival(opponent: dict[str, Any]) -> tuple[str, str]:
    """(id, nombre) del rival de una partida de ajedrez: el modelo o el algoritmo con su nivel."""
    kind = opponent.get("type")
    if kind == "llm":
        from .models import CATALOG_BY_ID, rival_label

        model = str(opponent.get("model") or "llm")
        label = rival_label(model) if model in CATALOG_BY_ID else str(opponent.get("modelo_etiqueta") or model)
        return model[:60], label[:120]
    if kind == "minimax":
        depth = int(opponent.get("depth") or 0)
        return f"minimax-{depth}", f"Minimax, profundidad {depth}"
    if kind == "stockfish":
        skill = int(opponent.get("skill") or 0)
        return f"stockfish-{skill}", f"Stockfish, nivel {skill}"
    names = {"random": "Aleatorio", "greedy": "Codicioso"}
    return str(kind)[:60], names.get(str(kind), str(kind))


class RankingStore:
    def __init__(self, db: Database, objects: ObjectStore, clock: Callable[[], float] = time.time) -> None:
        self.db = db
        self.objects = objects
        self.clock = clock
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[float, dict[str, Any]]] = {}

    # ---------------------------------------------------------------- escritura
    def _upsert_player(self, cur: Any, owner: str, name: str | None, public: bool) -> None:
        ts = self.clock()
        cur.execute(
            "UPDATE ranking_jugadores SET nombre = ?, publico = ?, actualizado = ? WHERE owner = ?",
            (name, 1 if public else 0, ts, owner),
        )
        if cur.rowcount == 0:
            cur.execute(
                "INSERT INTO ranking_jugadores (owner, nombre, publico, actualizado) VALUES (?, ?, ?, ?)",
                (owner, name, 1 if public else 0, ts),
            )

    def set_preference(self, user: User, public: bool) -> None:
        """Casilla «Salir en el ranking con mi nombre». Rehace las dos fotos fijas (cambia la lista de nombres)."""
        name = clean_name(user.name)

        def job(cur: Any) -> None:
            self._upsert_player(cur, user.owner, name, public)
            for demo in DEMOS:
                self._snapshot(cur, demo)

        self.db.submit(job)

    def record(
        self,
        demo: str,
        game_id: str,
        user: User,
        *,
        rival: str,
        rival_name: str,
        result: str,
        points: int | None = None,
        points_ai: int | None = None,
        public: bool | None = None,
    ) -> None:
        """Partida terminada. Idempotente: si ya estaba, no se duplica. `public` None = no tocar la preferencia."""
        if demo not in DEMOS or result not in RESULTS:
            raise ValueError(f"ranking: demo {demo!r} o resultado {result!r} no válidos")
        row_id = f"{demo}:{game_id}"[:40]
        ts = self.clock()
        name = clean_name(user.name)

        def job(cur: Any) -> None:
            if public is not None:
                self._upsert_player(cur, user.owner, name, public)
            else:
                cur.execute("UPDATE ranking_jugadores SET nombre = ? WHERE owner = ?", (name, user.owner))
                if cur.rowcount == 0:
                    self._upsert_player(cur, user.owner, name, True)
            # Una sola sentencia que no falla si la fila ya existe (no puede romper el lote de la cola).
            cur.execute(
                "INSERT INTO ranking_partidas (id, ts, demo, owner, rival, rival_nombre, resultado, puntos, puntos_ia) "
                "SELECT ?, ?, ?, ?, ?, ?, ?, ?, ? WHERE NOT EXISTS (SELECT 1 FROM ranking_partidas WHERE id = ?)",
                (row_id, ts, demo, user.owner, rival[:60], rival_name[:120], result, points, points_ai, row_id),
            )
            self._snapshot(cur, demo)

        self.db.submit(job)

    # ---------------------------------------------------------------- cálculo (dentro de la transacción)
    @staticmethod
    def _rows(cur: Any, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        cur.execute(sql, params)
        names = [d[0] for d in cur.description]
        return [dict(zip(names, row, strict=True)) for row in cur.fetchall()]

    def compute(self, cur: Any, demo: str) -> dict[str, Any]:
        top, lim = self.db.top(TOP_PLAYERS)
        if demo in ("geo", "geo-sv"):
            players = self._rows(
                cur,
                f"SELECT {top} j.nombre, p.puntos, p.puntos_ia, p.rival_nombre, p.ts "
                "FROM ranking_partidas p JOIN ranking_jugadores j ON j.owner = p.owner "
                "WHERE p.demo = ? AND j.publico = 1 AND j.nombre IS NOT NULL "
                f"ORDER BY p.puntos DESC, p.ts ASC {lim}",
                (demo,),
            )
            models = self._rows(
                cur,
                "SELECT rival, MAX(rival_nombre) AS rival_nombre, COUNT(*) AS partidas, "
                "AVG(CAST(puntos_ia AS FLOAT)) AS media_ia, AVG(CAST(puntos AS FLOAT)) AS media_humano, "
                "SUM(CASE WHEN resultado = 'victoria' THEN 1 ELSE 0 END) AS gana_humano, "
                "SUM(CASE WHEN resultado = 'derrota' THEN 1 ELSE 0 END) AS gana_ia, "
                "SUM(CASE WHEN resultado = 'tablas' THEN 1 ELSE 0 END) AS empates "
                "FROM ranking_partidas WHERE demo = ? GROUP BY rival",
                (demo,),
            )
            return {
                "jugadores": [
                    {
                        "nombre": r["nombre"],
                        "puntos": int(r["puntos"] or 0),
                        "puntos_ia": int(r["puntos_ia"] or 0),
                        "rival": r["rival_nombre"],
                        "fecha": _day(float(r["ts"])),
                    }
                    for r in players
                ],
                "modelos": sorted(
                    (
                        {
                            "id": r["rival"],
                            "rival": r["rival_nombre"],
                            "partidas": int(r["partidas"]),
                            "media_ia": round(float(r["media_ia"] or 0)),
                            "media_humano": round(float(r["media_humano"] or 0)),
                            "gana_humano": int(r["gana_humano"] or 0),
                            "gana_ia": int(r["gana_ia"] or 0),
                            "empates": int(r["empates"] or 0),
                        }
                        for r in models
                    ),
                    key=lambda m: (-m["media_ia"], -m["partidas"]),
                ),
            }
        wins = "SUM(CASE WHEN p.resultado = 'victoria' THEN 1 ELSE 0 END)"
        draws = "SUM(CASE WHEN p.resultado = 'tablas' THEN 1 ELSE 0 END)"
        losses = "SUM(CASE WHEN p.resultado = 'derrota' THEN 1 ELSE 0 END)"
        players = self._rows(
            cur,
            f"SELECT {top} j.nombre, COUNT(*) AS partidas, {wins} AS victorias, {draws} AS tablas, "
            f"{losses} AS derrotas FROM ranking_partidas p JOIN ranking_jugadores j ON j.owner = p.owner "
            "WHERE p.demo = 'meaculpa' AND j.publico = 1 AND j.nombre IS NOT NULL "
            f"GROUP BY j.owner, j.nombre ORDER BY {wins} DESC, {losses} ASC, COUNT(*) DESC {lim}",
        )
        rivals = self._rows(
            cur,
            f"SELECT p.rival, MAX(p.rival_nombre) AS rival_nombre, COUNT(*) AS partidas, {wins} AS victorias, "
            f"{draws} AS tablas, {losses} AS derrotas FROM ranking_partidas p WHERE p.demo = 'meaculpa' "
            "GROUP BY p.rival",
        )
        return {
            "jugadores": [
                {
                    "nombre": r["nombre"],
                    "partidas": int(r["partidas"]),
                    "victorias": int(r["victorias"] or 0),
                    "tablas": int(r["tablas"] or 0),
                    "derrotas": int(r["derrotas"] or 0),
                }
                for r in players
            ],
            "rivales": sorted(
                (
                    {
                        "id": r["rival"],
                        "rival": r["rival_nombre"],
                        "partidas": int(r["partidas"]),
                        # Desde el lado del humano: victorias = partidas que el humano le ganó a ese rival.
                        "victorias": int(r["victorias"] or 0),
                        "tablas": int(r["tablas"] or 0),
                        "derrotas": int(r["derrotas"] or 0),
                    }
                    for r in rivals
                ),
                key=lambda r: (-r["partidas"], r["rival"]),
            ),
        }

    def _snapshot(self, cur: Any, demo: str) -> None:
        """Calcula el ranking y guarda la foto fija en Blob. Un fallo de Blob no rompe el lote de la base de datos."""
        payload = {"demo": demo, "actualizado": self.clock(), **self.compute(cur, demo)}
        try:
            self._write_snapshot(demo, payload)
        except Exception:  # noqa: BLE001 - la foto fija se rehace en la siguiente partida
            log.exception("No se pudo guardar la foto fija del ranking %s", demo)
            return
        with self._lock:
            self._cache[demo] = (time.monotonic(), payload)

    def _write_snapshot(self, demo: str, payload: dict[str, Any]) -> None:
        key = f"ranking/{demo}.json"
        body = json.dumps(payload, ensure_ascii=False).encode()
        for _ in range(5):
            found = self.objects.read(key)
            if self.objects.write(key, body, etag=found[1] if found else None) is not None:
                return
        raise RuntimeError("conflicto al escribir la foto fija del ranking")

    def rebuild(self, demo: str) -> dict[str, Any]:
        """Recalcula la foto fija desde la base de datos (scripts y tests; despierta la base de datos)."""
        payload: dict[str, Any] = {}

        def job(cur: Any) -> None:
            self._snapshot(cur, demo)
            payload.update(self._cache.get(demo, (0, {}))[1])

        self.db.run(job)
        return payload

    # ---------------------------------------------------------------- lectura pública
    def public(self, demo: str) -> dict[str, Any]:
        with self._lock:
            hit = self._cache.get(demo)
        if hit and time.monotonic() - hit[0] < CACHE_SECONDS:
            return hit[1]
        found = self.objects.read(f"ranking/{demo}.json")
        if found:
            payload = json.loads(found[0])
        else:
            payload = {"demo": demo, "actualizado": None, "jugadores": []}
            payload["modelos" if demo.startswith("geo") else "rivales"] = []
        with self._lock:
            self._cache[demo] = (time.monotonic(), payload)
        return payload

    def export_rows(self) -> dict[str, list[dict[str, Any]]]:
        """Todas las filas (para migrar a otro almacén o hacer copia)."""
        return {
            "ranking_jugadores": self.db.query("SELECT owner, nombre, publico, actualizado FROM ranking_jugadores"),
            "ranking_partidas": self.db.query(
                "SELECT id, ts, demo, owner, rival, rival_nombre, resultado, puntos, puntos_ia FROM ranking_partidas"
            ),
        }


class PreferenceIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    publico: bool


def create_ranking_router(*, current_user: Callable[..., User], ranking: RankingStore) -> APIRouter:
    """GET públicos (sin login, solo nombres elegidos y cifras) y la casilla del jugador (con login)."""
    router = APIRouter(prefix="/api/ranking")

    @router.get("/{demo}")
    def read(demo: str, response: Response) -> dict[str, Any]:
        if demo not in DEMOS:
            response.status_code = 404
            return {"detail": "Ranking desconocido"}
        response.headers["Cache-Control"] = "public, max-age=60"
        return ranking.public(demo)

    @router.post("/preferencia")
    def preference(body: PreferenceIn, user: User = Depends(current_user)) -> dict[str, Any]:
        ranking.set_preference(user, body.publico)
        return {"publico": body.publico, "nombre": clean_name(user.name)}

    return router
