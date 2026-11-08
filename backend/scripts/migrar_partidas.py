"""Copia a la tabla `partidas` de Azure SQL las partidas que ya había en Blob (las dos demos). Idempotente.

    python backend/scripts/migrar_partidas.py            # muestra cuántas hay, sin escribir
    python backend/scripts/migrar_partidas.py --escribir

Variables: AZURE_STORAGE_ACCOUNT_URL, AZURE_STORAGE_CONTAINER (meaculpa), AZURE_SQL_SERVER y AZURE_SQL_DATABASE.
Hace falta `az login` con permiso de lectura de blobs (Storage Blob Data Reader) y ser admin de Entra del servidor
SQL (más una regla de firewall temporal para tu IP). La fecha de fin de las partidas terminadas es la última
modificación del blob: la mejor aproximación que hay para las anteriores a esta tabla.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from azure.identity import AzureCliCredential  # noqa: E402
from azure.storage.blob import ContainerClient  # noqa: E402
from sql_admin import connector  # noqa: E402

from meaculpa.db import Database  # noqa: E402
from meaculpa.games import GameRecord  # noqa: E402
from meaculpa.geo.store import GeoGame  # noqa: E402
from meaculpa.partidas import chess_row, geo_row, upsert_job  # noqa: E402


def main(write: bool) -> int:
    container = ContainerClient(
        os.environ["AZURE_STORAGE_ACCOUNT_URL"],
        os.environ.get("AZURE_STORAGE_CONTAINER", "meaculpa"),
        credential=AzureCliCredential(),
    )
    rows = []
    for blob in container.list_blobs():
        parts = blob.name.split("/")
        modified = blob.last_modified.timestamp()
        raw = None
        if parts[0] == "games" and len(parts) == 3 and parts[2].endswith(".json"):
            raw = container.download_blob(blob.name).readall()
            game = GameRecord(**json.loads(raw.decode("utf-8")))
            rows.append(chess_row(game, modified))
        elif parts[0] == "geo" and len(parts) == 3 and parts[2] != "vistas" and parts[2].isalnum():
            raw = container.download_blob(blob.name).readall()
            rows.append(geo_row(GeoGame.from_json(raw, None), modified))
    by_demo: dict[str, int] = {}
    for r in rows:
        by_demo[r["demo"]] = by_demo.get(r["demo"], 0) + 1
    print("Partidas en Blob:", by_demo or "ninguna")
    if write and rows:
        db = Database(connector(), "mssql", background=False)
        db.run(lambda cur: [upsert_job(r)(cur) for r in rows])
        print(f"Escritas {len(rows)} filas en partidas")
    return 0


if __name__ == "__main__":
    raise SystemExit(main("--escribir" in sys.argv[1:]))
