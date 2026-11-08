"""Conexión a la base de datos y migraciones.

- En Azure: Azure SQL Database (oferta gratuita, serverless) con pyodbc y ODBC Driver 18. Sin contraseñas: se
  autentica con un token de Microsoft Entra de la identidad administrada (o de `az login` en local).
- Sin SQL configurado (local y tests): SQLite en un fichero o en memoria, con las mismas sentencias.

La base de datos serverless se pausa sola cuando nadie la usa y, si se agota el límite gratuito del mes, se queda
pausada hasta el mes siguiente. Por eso nada de esto puede tumbar las demos:
- las escrituras de uso van a una cola con un hilo que las agrupa y reintenta (y si no puede, las descarta);
- las lecturas fallan rápido (`DatabaseUnavailable`) y durante 30 s no se vuelve a intentar (cortacircuitos);
- no hay conexiones abiertas en reposo ni consultas periódicas: cada operación abre y cierra su conexión,
  para no impedir que la base de datos se pause.

Migraciones: ficheros `migrations/NNN_nombre.sql` versionados, aplicados en orden y anotados en
`schema_migraciones`. La identidad de la API solo lee y escribe datos; las migraciones las aplica el
administrador con `python backend/scripts/sql_admin.py migrar`.
"""

from __future__ import annotations

import logging
import queue
import re
import sqlite3
import struct
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any

log = logging.getLogger("meaculpa.db")

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
SQL_SCOPE = "https://database.windows.net/.default"
SQL_COPT_SS_ACCESS_TOKEN = 1256  # atributo de conexión de ODBC Driver 17/18 para el token de Entra

Job = Callable[[Any], Any]  # recibe un cursor


class DatabaseUnavailable(Exception):
    """La base de datos no responde (pausada, sin red o sin límite gratuito): se sigue sin ella."""


def token_struct(token: str) -> bytes:
    raw = token.encode("utf-16-le")
    return struct.pack(f"<I{len(raw)}s", len(raw), raw)


def mssql_connector(server: str, database: str, credential: Any, timeout: int = 8) -> Callable[[], Any]:
    """Conexiones a Azure SQL con pyodbc + ODBC Driver 18 y token de Entra (sin usuario ni contraseña)."""
    import pyodbc

    pyodbc.pooling = False  # sin conexiones abiertas en reposo: la base de datos serverless puede pausarse
    conn_str = (
        "Driver={ODBC Driver 18 for SQL Server};"
        f"Server=tcp:{server},1433;Database={database};"
        f"Encrypt=yes;TrustServerCertificate=no;Connection Timeout={timeout}"
    )

    def connect() -> Any:
        token = credential.get_token(SQL_SCOPE).token  # azure-identity lo cachea hasta que caduca
        return pyodbc.connect(conn_str, attrs_before={SQL_COPT_SS_ACCESS_TOKEN: token_struct(token)}, timeout=timeout)

    return connect


def split_statements(sql: str) -> list[str]:
    """Parte un fichero de migración en sentencias (terminadas en ';' a final de línea), sin comentarios."""
    body = "\n".join(line for line in sql.splitlines() if not line.strip().startswith("--"))
    return [s.strip() for s in re.split(r";\s*$", body, flags=re.M) if s.strip()]


def migration_files() -> list[tuple[int, Path]]:
    out = []
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        m = re.match(r"(\d+)_", path.name)
        if m:
            out.append((int(m.group(1)), path))
    return out


class Database:
    """Acceso mínimo con el mismo SQL en los dos motores (marcadores `?`, que usan pyodbc y sqlite3)."""

    RETRY_DELAYS = (5.0, 30.0)
    BREAKER_SECONDS = 30.0

    def __init__(self, connect: Callable[[], Any], dialect: str, *, background: bool = True) -> None:
        if dialect not in {"mssql", "sqlite"}:
            raise ValueError(dialect)
        self._connect = connect
        self.dialect = dialect
        self.background = background
        self._lock = threading.Lock()  # SQLite: una sola conexión compartida
        self._shared: Any = None
        self._down_until = 0.0
        self.last_ok = -1e9  # time.monotonic() del último acceso correcto: si es reciente, está despierta
        self._queue: queue.Queue[Job] = queue.Queue(maxsize=10_000)
        self._writer: threading.Thread | None = None

    @classmethod
    def sqlite(cls, path: str | Path = ":memory:") -> Database:
        def connect() -> sqlite3.Connection:
            return sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)

        db = cls(connect, "sqlite", background=False)
        db.migrate()  # en local las migraciones se aplican solas
        return db

    # ---------------------------------------------------------------- conexión
    @contextmanager
    def _connection(self) -> Iterator[Any]:
        if self.dialect == "sqlite":
            with self._lock:
                if self._shared is None:
                    self._shared = self._connect()
                yield self._shared
            return
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    def _transaction(self, job: Job) -> Any:
        with self._connection() as conn:
            cur = conn.cursor()
            if self.dialect == "sqlite":
                cur.execute("BEGIN")
            try:
                result = job(cur)
            except BaseException:
                conn.rollback()
                raise
            conn.commit()
            self.last_ok = time.monotonic()
            return result

    def run(self, job: Job, *, retries: int = 0, wait: float = 10.0) -> Any:
        """Ejecuta `job(cursor)` en una transacción. Si la base de datos no responde: DatabaseUnavailable.

        `retries`: reintentos esperando `wait` s (lo que tarda en despertar si estaba pausada), solo para
        acciones de una persona que espera la respuesta, como enviar el formulario de contacto.
        """
        if self.dialect == "sqlite":  # local y tests: un error de SQL es un fallo de verdad, no se oculta
            return self._transaction(job)
        if not retries and time.monotonic() < self._down_until:
            raise DatabaseUnavailable("base de datos no disponible (reintento en unos segundos)")
        for attempt in range(retries + 1):
            try:
                result = self._transaction(job)
                self._down_until = 0.0
                return result
            except Exception as exc:
                log.warning("Base de datos (intento %d): %s", attempt + 1, exc)
                if attempt == retries:
                    self._down_until = time.monotonic() + self.BREAKER_SECONDS
                    raise DatabaseUnavailable(str(exc)) from exc
                time.sleep(wait)
        raise AssertionError("inalcanzable")

    def query(self, sql: str, params: tuple | list = ()) -> list[dict[str, Any]]:
        def job(cur: Any) -> list[dict[str, Any]]:
            cur.execute(sql, tuple(params))
            names = [d[0] for d in cur.description]
            return [dict(zip(names, row, strict=True)) for row in cur.fetchall()]

        return self.run(job)

    def execute(self, sql: str, params: tuple | list = ()) -> int:
        def job(cur: Any) -> int:
            cur.execute(sql, tuple(params))
            return cur.rowcount

        return self.run(job)

    def top(self, n: int) -> tuple[str, str]:
        """(prefijo, sufijo) para limitar filas: TOP (n) en T-SQL, LIMIT n en SQLite."""
        n = int(n)
        return (f"TOP ({n})", "") if self.dialect == "mssql" else ("", f"LIMIT {n}")

    # ---------------------------------------------------------------- escrituras en segundo plano
    def submit(self, job: Job) -> None:
        """Escritura que no debe frenar la petición (uso, visitas). En SQLite se hace en el momento."""
        if not self.background:
            with suppress(DatabaseUnavailable):
                self.run(job)
            return
        if self._writer is None or not self._writer.is_alive():
            self._writer = threading.Thread(target=self._write_loop, name="db-writer", daemon=True)
            self._writer.start()
        try:
            self._queue.put_nowait(job)
        except queue.Full:
            log.warning("Cola de escrituras llena: se descarta un registro de uso")

    def _write_loop(self) -> None:
        while True:
            batch = [self._queue.get()]
            while len(batch) < 500:
                try:
                    batch.append(self._queue.get_nowait())
                except queue.Empty:
                    break
            for attempt, delay in enumerate((*self.RETRY_DELAYS, None)):
                try:
                    self._transaction(lambda cur, jobs=batch: [job(cur) for job in jobs])
                    break
                except Exception as exc:  # noqa: BLE001 - pausada, despertando o sin límite gratuito
                    if delay is None:
                        log.warning("Se descartan %d registros de uso: %s", len(batch), exc)
                    else:
                        log.info("Base de datos ocupada (intento %d): %s", attempt + 1, exc)
                        time.sleep(delay)

    def recently_awake(self, seconds: float = 300.0) -> bool:
        """Si ha habido un acceso correcto hace poco: la base de datos serverless está despierta y escribir no
        la despierta (ni gasta más límite gratuito del que ya se está gastando)."""
        return time.monotonic() - self.last_ok < seconds

    def flush(self, timeout: float = 10.0) -> None:
        """Espera a que se vacíe la cola (para scripts y tests)."""
        end = time.monotonic() + timeout
        while not self._queue.empty() and time.monotonic() < end:
            time.sleep(0.05)

    # ---------------------------------------------------------------- migraciones
    def _ensure_migrations_table(self, cur: Any) -> None:
        if self.dialect == "mssql":
            cur.execute(
                "IF OBJECT_ID('dbo.schema_migraciones', 'U') IS NULL "
                "CREATE TABLE schema_migraciones (version INT NOT NULL PRIMARY KEY, nombre NVARCHAR(200) NOT NULL, "
                "aplicada FLOAT NOT NULL)"
            )
        else:
            cur.execute(
                "CREATE TABLE IF NOT EXISTS schema_migraciones (version INT NOT NULL PRIMARY KEY, "
                "nombre NVARCHAR(200) NOT NULL, aplicada FLOAT NOT NULL)"
            )

    def applied_versions(self) -> set[int]:
        def job(cur: Any) -> set[int]:
            self._ensure_migrations_table(cur)
            cur.execute("SELECT version FROM schema_migraciones")
            return {int(r[0]) for r in cur.fetchall()}

        return self._transaction(job)

    def migrate(self) -> list[str]:
        """Aplica las migraciones pendientes, cada una en su transacción. Devuelve las aplicadas."""
        done = self.applied_versions()
        applied = []
        for version, path in migration_files():
            if version in done:
                continue
            statements = split_statements(path.read_text(encoding="utf-8"))
            if self.dialect == "sqlite":  # SQLite no entiende NVARCHAR(MAX); TEXT no tiene límite
                statements = [s.replace("NVARCHAR(MAX)", "TEXT") for s in statements]

            def job(cur: Any, statements=statements, version=version, name=path.name) -> None:
                for stmt in statements:
                    cur.execute(stmt)
                cur.execute(
                    "INSERT INTO schema_migraciones (version, nombre, aplicada) VALUES (?, ?, ?)",
                    (version, name, time.time()),
                )

            self._transaction(job)
            applied.append(path.name)
        return applied
