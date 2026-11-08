"""Datos de las demos en la base de datos: usuarios, uso, mensajes de contacto y controles del panel /admin.

Las partidas siguen en Blob (storage.py); aquí solo va lo que el panel necesita para contar y decidir.
Todo lo que se consulta en cada petición (bloqueos y configuración) se cachea en memoria unos segundos y, si la
base de datos no responde, se usa lo último conocido o el valor por defecto: las demos nunca dependen de ella.
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
import uuid
from typing import Any

from .db import Database, DatabaseUnavailable
from .partidas import upsert_job

log = logging.getLogger("meaculpa.datos")

DEMOS = {"meaculpa": "Mea culpa (ajedrez)", "geo": "¿Dónde está esta foto?"}
MESSAGE_RETENTION_S = 365 * 86400  # 12 meses


def day_of(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(ts))


def _num(value: Any) -> float:
    return float(value or 0)


class DataStore:
    CONFIG_TTL = 60.0  # s que vale la configuración en memoria
    USER_TTL = 120.0  # s entre comprobaciones del bloqueo (y escrituras de última visita) de un usuario

    def __init__(self, db: Database, clock=time.time) -> None:
        self.db = db
        self.clock = clock
        self._lock = threading.Lock()
        self._config: dict[str, str] = {}
        self._config_at = -1e9
        self._users: dict[str, tuple[bool, float]] = {}

    # ---------------------------------------------------------------- usuarios
    def touch_user(self, owner: str, email: str | None, name: str | None) -> bool:
        """Registra la visita (como mucho cada USER_TTL s) y devuelve si el usuario está bloqueado."""
        now = time.monotonic()
        with self._lock:
            cached = self._users.get(owner)
        if cached and now - cached[1] < self.USER_TTL:
            return cached[0]
        blocked = cached[0] if cached else False
        try:
            rows = self.db.query("SELECT bloqueado FROM usuarios WHERE owner = ?", (owner,))
            blocked = bool(rows[0]["bloqueado"]) if rows else False
        except DatabaseUnavailable:
            pass
        with self._lock:
            self._users[owner] = (blocked, now)
        ts = self.clock()
        email = (email or "")[:320] or None
        name = (name or "")[:60] or None

        def job(cur: Any) -> None:
            cur.execute(
                "UPDATE usuarios SET ultima_visita = ?, email = ?, nombre = ? WHERE owner = ?", (ts, email, name, owner)
            )
            if cur.rowcount == 0:
                cur.execute(
                    "INSERT INTO usuarios (owner, email, nombre, primera_visita, ultima_visita, bloqueado) "
                    "VALUES (?, ?, ?, ?, ?, 0)",
                    (owner, email, name, ts, ts),
                )

        self.db.submit(job)
        return blocked

    def set_blocked(self, owner: str, blocked: bool) -> bool:
        n = self.db.execute("UPDATE usuarios SET bloqueado = ? WHERE owner = ?", (1 if blocked else 0, owner))
        with self._lock:
            self._users[owner] = (blocked, time.monotonic())
        return n > 0

    def user_email(self, owner: str) -> str | None:
        rows = self.db.query("SELECT email FROM usuarios WHERE owner = ?", (owner,))
        return rows[0]["email"] if rows else None

    def users(self, limit: int = 200) -> list[dict[str, Any]]:
        top, lim = self.db.top(limit)
        rows = self.db.query(
            f"SELECT {top} u.owner, u.email, u.nombre, u.primera_visita, u.ultima_visita, u.bloqueado, "
            "COUNT(e.id) AS eventos, COALESCE(SUM(e.coste_usd), 0) AS coste_usd "
            "FROM usuarios u LEFT JOIN eventos e ON e.owner = u.owner "
            "GROUP BY u.owner, u.email, u.nombre, u.primera_visita, u.ultima_visita, u.bloqueado "
            f"ORDER BY u.ultima_visita DESC {lim}"
        )
        return [
            {**r, "bloqueado": bool(r["bloqueado"]), "eventos": int(r["eventos"]), "coste_usd": _num(r["coste_usd"])}
            for r in rows
        ]

    # ---------------------------------------------------------------- eventos de uso
    def record(
        self,
        owner: str | None,
        demo: str,
        tipo: str,
        *,
        ref: str | None = None,
        modelo: str | None = None,
        tokens_in: int = 0,
        tokens_out: int = 0,
        usd: float = 0.0,
    ) -> None:
        ts = self.clock()
        row = (
            uuid.uuid4().hex,
            ts,
            day_of(ts),
            owner,
            demo,
            tipo,
            ref,
            (modelo or None) and str(modelo)[:60],
            int(tokens_in or 0),
            int(tokens_out or 0),
            float(usd or 0.0),
        )
        self.db.submit(
            lambda cur: cur.execute(
                "INSERT INTO eventos (id, ts, dia, owner, demo, tipo, ref, modelo, tokens_entrada, tokens_salida, "
                "coste_usd) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                row,
            )
        )

    # ---------------------------------------------------------------- configuración (controles)
    def config(self) -> dict[str, str]:
        now = time.monotonic()
        if now - self._config_at < self.CONFIG_TTL:
            return self._config
        try:
            rows = self.db.query("SELECT clave, valor FROM config")
            self._config = {r["clave"]: r["valor"] for r in rows}
        except DatabaseUnavailable:
            pass  # lo último conocido (o nada: valores por defecto)
        self._config_at = now
        return self._config

    def set_config(self, key: str, value: str) -> None:
        ts = self.clock()

        def job(cur: Any) -> None:
            cur.execute("UPDATE config SET valor = ?, actualizado = ? WHERE clave = ?", (value, ts, key))
            if cur.rowcount == 0:
                cur.execute("INSERT INTO config (clave, valor, actualizado) VALUES (?, ?, ?)", (key, value, ts))

        self.db.run(job)
        self._config_at = -1e9  # se relee en la próxima consulta

    def delete_config(self, key: str) -> None:
        self.db.execute("DELETE FROM config WHERE clave = ?", (key,))
        self._config_at = -1e9

    def demo_enabled(self, demo: str) -> bool:
        return self.config().get(f"demo.{demo}", "1") != "0"

    def model_enabled(self, model_id: str) -> bool:
        return self.config().get(f"modelo.{model_id}", "1") != "0"

    def quota(self, kind: str, default: int) -> int:
        raw = self.config().get(f"cuota.{kind}")
        try:
            return int(raw) if raw is not None else default
        except ValueError:
            return default

    # ---------------------------------------------------------------- formulario de contacto
    def contact_slot(self, ip: str, per_ip: int, per_day: int) -> bool:
        """Cuenta un envío para esta IP y para el día. False si se pasa de algún límite.

        La IP no se guarda: solo un hash con el día, y las filas de días anteriores se borran aquí mismo.
        """
        today = day_of(self.clock())
        ip_key = hashlib.sha256(f"contacto:{today}:{ip}".encode()).hexdigest()
        day_key = f"total:{today}"

        def job(cur: Any) -> bool:
            cur.execute("DELETE FROM limites_contacto WHERE dia <> ?", (today,))
            for key, limit in ((ip_key, per_ip), (day_key, per_day)):
                cur.execute("SELECT cuenta FROM limites_contacto WHERE clave = ?", (key,))
                row = cur.fetchone()
                if row and int(row[0]) >= limit:
                    return False
            for key in (ip_key, day_key):
                cur.execute("UPDATE limites_contacto SET cuenta = cuenta + 1 WHERE clave = ?", (key,))
                if cur.rowcount == 0:
                    cur.execute("INSERT INTO limites_contacto (clave, dia, cuenta) VALUES (?, ?, 1)", (key, today))
            return True

        return bool(self.db.run(job, retries=3))

    def add_message(self, nombre: str, email: str, mensaje: str) -> str:
        msg_id = uuid.uuid4().hex
        ts = self.clock()

        def job(cur: Any) -> None:
            cur.execute("DELETE FROM mensajes WHERE ts < ?", (ts - MESSAGE_RETENTION_S,))  # conservación: 12 meses
            cur.execute(
                "INSERT INTO mensajes (id, ts, nombre, email, mensaje, leido) VALUES (?, ?, ?, ?, ?, 0)",
                (msg_id, ts, nombre, email, mensaje),
            )

        self.db.run(job)
        return msg_id

    def messages(self, limit: int = 200) -> list[dict[str, Any]]:
        top, lim = self.db.top(limit)
        rows = self.db.query(f"SELECT {top} id, ts, nombre, email, mensaje, leido FROM mensajes ORDER BY ts DESC {lim}")
        return [{**r, "leido": bool(r["leido"])} for r in rows]

    def set_read(self, msg_id: str, leido: bool) -> bool:
        return self.db.execute("UPDATE mensajes SET leido = ? WHERE id = ?", (1 if leido else 0, msg_id)) > 0

    def delete_message(self, msg_id: str) -> bool:
        return self.db.execute("DELETE FROM mensajes WHERE id = ?", (msg_id,)) > 0

    # ---------------------------------------------------------------- estadísticas del panel
    def summary(self) -> dict[str, Any]:
        now = self.clock()
        today = day_of(now)
        month = today[:8] + "01"
        u = self.db.query(
            "SELECT COUNT(*) AS total, "
            "SUM(CASE WHEN bloqueado = 1 THEN 1 ELSE 0 END) AS bloqueados, "
            "SUM(CASE WHEN ultima_visita >= ? THEN 1 ELSE 0 END) AS activos_24h, "
            "SUM(CASE WHEN ultima_visita >= ? THEN 1 ELSE 0 END) AS activos_7d, "
            "SUM(CASE WHEN ultima_visita >= ? THEN 1 ELSE 0 END) AS activos_30d, "
            "SUM(CASE WHEN primera_visita >= ? THEN 1 ELSE 0 END) AS nuevos_7d FROM usuarios",
            (now - 86400, now - 7 * 86400, now - 30 * 86400, now - 7 * 86400),
        )[0]
        e = self.db.query(
            "SELECT "
            "SUM(CASE WHEN dia = ? THEN 1 ELSE 0 END) AS eventos_hoy, "
            "SUM(CASE WHEN tipo IN ('partida', 'geo_partida') THEN 1 ELSE 0 END) AS partidas_mes, "
            "SUM(coste_usd) AS coste_ia_mes, SUM(tokens_entrada) AS tokens_entrada_mes, "
            "SUM(tokens_salida) AS tokens_salida_mes FROM eventos WHERE dia >= ?",
            (today, month),
        )[0]
        m = self.db.query("SELECT COUNT(*) AS sin_leer FROM mensajes WHERE leido = 0")[0]
        return {
            "usuarios": {k: int(v or 0) for k, v in u.items()},
            "eventos_hoy": int(e["eventos_hoy"] or 0),
            "partidas_mes": int(e["partidas_mes"] or 0),
            "coste_ia_mes_usd": round(_num(e["coste_ia_mes"]), 4),
            "tokens_mes": {"entrada": int(e["tokens_entrada_mes"] or 0), "salida": int(e["tokens_salida_mes"] or 0)},
            "mensajes_sin_leer": int(m["sin_leer"] or 0),
            "mes": month[:7],
        }

    def usage(self, days: int = 30) -> list[dict[str, Any]]:
        """Por día y demo: usuarios distintos y número de eventos de cada tipo."""
        since = day_of(self.clock() - (days - 1) * 86400)
        counts = self.db.query(
            "SELECT dia, demo, tipo, COUNT(*) AS n FROM eventos WHERE dia >= ? GROUP BY dia, demo, tipo", (since,)
        )
        people = self.db.query(
            "SELECT dia, demo, COUNT(DISTINCT owner) AS u FROM eventos WHERE dia >= ? GROUP BY dia, demo", (since,)
        )
        out: dict[tuple[str, str], dict[str, Any]] = {}
        for r in people:
            out[(r["dia"], r["demo"])] = {"dia": r["dia"], "demo": r["demo"], "usuarios": int(r["u"]), "eventos": {}}
        for r in counts:
            row = out.setdefault(
                (r["dia"], r["demo"]), {"dia": r["dia"], "demo": r["demo"], "usuarios": 0, "eventos": {}}
            )
            row["eventos"][r["tipo"]] = int(r["n"])
        return sorted(out.values(), key=lambda r: (r["dia"], r["demo"]), reverse=True)

    def save_game(self, row: dict[str, Any]) -> None:
        """Fila de `partidas` (partidas.chess_row / geo_row), por la cola en segundo plano."""
        self.db.submit(upsert_job(row))

    def recent_games(self, limit: int = 50) -> list[dict[str, Any]]:
        top, lim = self.db.top(limit)
        games = self.db.query(
            f"SELECT {top} p.id, p.demo, p.rival, p.rival_nombre, p.estado, p.resultado, p.motivo_fin, p.jugadas, "
            "p.puntos, p.puntos_ia, p.inicio, p.duracion_s, u.email, u.nombre FROM partidas p "
            f"LEFT JOIN usuarios u ON u.owner = p.owner ORDER BY p.inicio DESC {lim}"
        )
        refs = [g["id"].split(":", 1)[-1] for g in games]
        stats: dict[str, dict[str, Any]] = {}
        if refs:
            marks = ", ".join("?" for _ in refs)
            for r in self.db.query(
                "SELECT ref, SUM(coste_usd) AS c, SUM(CASE WHEN tipo = 'analisis' THEN 1 ELSE 0 END) AS a "
                f"FROM eventos WHERE ref IN ({marks}) GROUP BY ref",
                refs,
            ):
                stats[r["ref"]] = {"coste_usd": round(_num(r["c"]), 6), "analisis": int(r["a"] or 0)}
        out = []
        for g, ref in zip(games, refs, strict=True):
            extra = stats.get(ref, {"coste_usd": 0.0, "analisis": 0})
            out.append({**g, "ref": ref, "ts": g["inicio"], "modelo": g["rival"], **extra})
        return out

    def games_by_model(self, days: int = 30) -> list[dict[str, Any]]:
        """Partidas por demo y rival (modelo o algoritmo) desde hace `days` días, con resultados del humano."""
        since = day_of(self.clock() - (days - 1) * 86400)
        rows = self.db.query(
            "SELECT demo, rival, rival_nombre, COUNT(*) AS partidas, "
            "SUM(CASE WHEN estado = 'terminada' THEN 1 ELSE 0 END) AS terminadas, "
            "SUM(CASE WHEN resultado = 'victoria' THEN 1 ELSE 0 END) AS victorias, "
            "SUM(CASE WHEN resultado = 'tablas' THEN 1 ELSE 0 END) AS tablas, "
            "SUM(CASE WHEN resultado = 'derrota' THEN 1 ELSE 0 END) AS derrotas, "
            "AVG(CASE WHEN estado = 'terminada' THEN duracion_s END) AS duracion_media "
            "FROM partidas WHERE dia >= ? GROUP BY demo, rival, rival_nombre",
            (since,),
        )
        out = [
            {
                **r,
                **{k: int(r[k] or 0) for k in ("partidas", "terminadas", "victorias", "tablas", "derrotas")},
                "duracion_media": round(_num(r["duracion_media"]), 1) if r["duracion_media"] is not None else None,
            }
            for r in rows
        ]
        return sorted(out, key=lambda r: (r["demo"], -r["partidas"]))

    def ai_spend(self, days: int = 30) -> dict[str, Any]:
        since = day_of(self.clock() - (days - 1) * 86400)
        rows = self.db.query(
            "SELECT dia, modelo, COUNT(*) AS llamadas, SUM(tokens_entrada) AS tin, SUM(tokens_salida) AS tout, "
            "SUM(coste_usd) AS usd FROM eventos WHERE dia >= ? AND modelo IS NOT NULL "
            "AND (tokens_entrada > 0 OR tokens_salida > 0 OR coste_usd > 0) GROUP BY dia, modelo",
            (since,),
        )
        per_day = [
            {
                "dia": r["dia"],
                "modelo": r["modelo"],
                "llamadas": int(r["llamadas"]),
                "tokens_entrada": int(r["tin"] or 0),
                "tokens_salida": int(r["tout"] or 0),
                "coste_usd": round(_num(r["usd"]), 6),
            }
            for r in rows
        ]
        per_model: dict[str, dict[str, Any]] = {}
        for r in per_day:
            t = per_model.setdefault(
                r["modelo"],
                {"modelo": r["modelo"], "llamadas": 0, "tokens_entrada": 0, "tokens_salida": 0, "coste_usd": 0.0},
            )
            for k in ("llamadas", "tokens_entrada", "tokens_salida", "coste_usd"):
                t[k] += r[k]
        return {
            "desde": since,
            "por_dia": sorted(per_day, key=lambda r: (r["dia"], r["modelo"]), reverse=True),
            "por_modelo": sorted(per_model.values(), key=lambda r: r["coste_usd"], reverse=True),
            "total_usd": round(sum(r["coste_usd"] for r in per_day), 6),
        }
