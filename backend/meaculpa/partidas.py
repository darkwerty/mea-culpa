"""Filas de la tabla `partidas` (migración 003) a partir de las partidas de las dos demos.

Lo usan la API (en cada guardado de una partida, por la cola en segundo plano de db.py) y el script de migración
de lo que ya había en Blob (`backend/scripts/migrar_partidas.py`). La partida en sí se sigue leyendo de Blob.
"""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any

from .ranking import chess_rival

if TYPE_CHECKING:  # solo tipos: geo importa datos, que importa este módulo
    from .games import GameRecord
    from .geo.store import GeoGame

COLUMNS = (
    "id",
    "demo",
    "owner",
    "rival",
    "rival_nombre",
    "estado",
    "resultado",
    "motivo_fin",
    "color",
    "jugadas",
    "pgn",
    "rondas",
    "puntos",
    "puntos_ia",
    "inicio",
    "actualizado",
    "fin",
    "duracion_s",
    "dia",
)


def _day(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(ts))


def chess_row(game: GameRecord, now: float) -> dict[str, Any]:
    rival, rival_name = chess_rival(game.opponent)
    done = game.status == "terminada"
    return {
        "id": f"meaculpa:{game.id}"[:40],
        "demo": "meaculpa",
        "owner": game.owner,
        "rival": rival[:60],
        "rival_nombre": rival_name[:120],
        "estado": game.status,
        "resultado": game.resultado_usuario if done else None,
        "motivo_fin": (game.motivo_fin or None) and str(game.motivo_fin)[:40],
        "color": game.user_color,
        "jugadas": len(game.moves),
        "pgn": game.pgn(),
        "rondas": None,
        "puntos": None,
        "puntos_ia": None,
        "inicio": game.created,
        "actualizado": now,
        "fin": now if done else None,
        "duracion_s": round(max(0.0, now - game.created), 1),
        "dia": _day(game.created),
    }


def geo_row(game: GeoGame, now: float, rival_name: str | None = None) -> dict[str, Any]:
    rounds = []
    for r in game.rondas:
        ia = r.get("ia") or {}
        rounds.append(
            {
                "jugador": r["jugador"].get("puntos", 0),
                "ia": ia.get("puntos", 0) if ia else None,
                "km_jugador": r["jugador"].get("distancia_km"),
                "km_ia": ia.get("distancia_km") if ia else None,
            }
        )
    you = sum(x["jugador"] for x in rounds)
    ai = sum(x["ia"] or 0 for x in rounds)
    done = game.estado == "terminada"
    if rival_name is None:
        from .models import rival_label

        rival_name = rival_label(game.modelo)
    return {
        "id": f"geo:{game.id}"[:40],
        "demo": "geo",
        "owner": game.owner,
        "rival": game.modelo[:60],
        "rival_nombre": rival_name[:120],
        "estado": game.estado,
        "resultado": ("victoria" if you > ai else "derrota" if you < ai else "tablas") if done else None,
        "motivo_fin": None,
        "color": None,
        "jugadas": len(game.rondas),
        "pgn": None,
        "rondas": json.dumps(rounds),
        "puntos": you,
        "puntos_ia": ai,
        "inicio": game.created,
        "actualizado": now,
        "fin": now if done else None,
        "duracion_s": round(max(0.0, now - game.created), 1),
        "dia": _day(game.created),
    }


def upsert_job(row: dict[str, Any]):
    """Trabajo para la cola de db.py: actualiza la fila (sin mover el fin ya fijado) o la crea."""
    sets = [c for c in COLUMNS if c not in {"id", "inicio", "dia", "owner", "demo", "fin", "duracion_s"}]

    def job(cur: Any) -> None:
        cur.execute(
            "UPDATE partidas SET "
            + ", ".join(f"{c} = ?" for c in sets)
            + ", duracion_s = CASE WHEN fin IS NULL THEN ? ELSE duracion_s END, fin = COALESCE(fin, ?) WHERE id = ?",
            (*[row[c] for c in sets], row["duracion_s"], row["fin"], row["id"]),
        )
        if cur.rowcount == 0:
            cur.execute(
                f"INSERT INTO partidas ({', '.join(COLUMNS)}) VALUES ({', '.join('?' for _ in COLUMNS)})",
                tuple(row[c] for c in COLUMNS),
            )

    return job
