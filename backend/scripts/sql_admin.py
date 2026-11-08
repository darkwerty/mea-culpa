"""Administración de Azure SQL desde tu equipo, con tu sesión de `az login` (administrador de Entra del servidor).

    python backend/scripts/sql_admin.py migrar                      # aplica migrations/NNN_*.sql pendientes
    python backend/scripts/sql_admin.py sql infra/sql/identidad_api.sql   # usuario y permisos de la identidad
    python backend/scripts/sql_admin.py consulta "SELECT COUNT(*) AS n FROM mensajes"

Variables: AZURE_SQL_SERVER (p. ej. meaculpa-sql-fc.database.windows.net) y AZURE_SQL_DATABASE (meaculpa).
Driver: pyodbc + ODBC Driver 18 si está instalado; si no, mssql-python (`pip install mssql-python`), que trae el
suyo. No hay contraseñas: el servidor solo admite autenticación de Microsoft Entra.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from meaculpa.db import SQL_COPT_SS_ACCESS_TOKEN, SQL_SCOPE, Database, split_statements, token_struct  # noqa: E402


def connector():
    from azure.identity import AzureCliCredential

    server = os.environ["AZURE_SQL_SERVER"]
    database = os.environ["AZURE_SQL_DATABASE"]
    credential = AzureCliCredential()
    conn_str = f"Server=tcp:{server},1433;Database={database};Encrypt=yes;TrustServerCertificate=no"
    try:
        import pyodbc

        if "ODBC Driver 18 for SQL Server" in pyodbc.drivers():

            def connect_pyodbc():
                token = token_struct(credential.get_token(SQL_SCOPE).token)
                return pyodbc.connect(
                    "Driver={ODBC Driver 18 for SQL Server};" + conn_str, attrs_before={SQL_COPT_SS_ACCESS_TOKEN: token}
                )

            return connect_pyodbc
    except ImportError:
        pass
    import mssql_python

    def connect_mssql_python():
        token = token_struct(credential.get_token(SQL_SCOPE).token)
        return mssql_python.connect(conn_str, attrs_before={SQL_COPT_SS_ACCESS_TOKEN: token})

    return connect_mssql_python


def main(argv: list[str]) -> int:
    if not argv or argv[0] not in {"migrar", "sql", "consulta"}:
        print(__doc__)
        return 2
    db = Database(connector(), "mssql", background=False)
    if argv[0] == "migrar":
        applied = db.migrate()
        print("Aplicadas:", ", ".join(applied) if applied else "ninguna (ya estaba al día)")
        print("Versiones:", sorted(db.applied_versions()))
    elif argv[0] == "sql":
        statements = split_statements(Path(argv[1]).read_text(encoding="utf-8"))
        db.run(lambda cur: [cur.execute(s) for s in statements])
        print(f"Ejecutadas {len(statements)} sentencias de {argv[1]}")
    else:
        rows = db.query(argv[1], argv[2:])
        print(json.dumps(rows, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
