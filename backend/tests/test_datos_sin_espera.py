"""La base de datos serverless puede estar pausada: las peticiones no deben esperar a que despierte."""

from __future__ import annotations

import threading
import time

from meaculpa.datos import DataStore
from meaculpa.db import DatabaseUnavailable


class SlowDB:
    """Imita Azure SQL: escrituras en segundo plano y lecturas que tardan `delay` s (o fallan)."""

    background = True

    def __init__(self, delay: float, config: dict[str, str] | None = None, fail: bool = False) -> None:
        self.delay = delay
        self.config = config or {}
        self.fail = fail
        self.queries = 0
        self.lock = threading.Lock()

    def query(self, sql: str, params: tuple = ()) -> list[dict]:
        with self.lock:
            self.queries += 1
        time.sleep(self.delay)
        if self.fail:
            raise DatabaseUnavailable("pausada")
        if "FROM config" in sql:
            return [{"clave": k, "valor": v} for k, v in self.config.items()]
        if "FROM usuarios" in sql:
            return [{"bloqueado": 1}]
        return []

    def submit(self, job) -> None:  # noqa: ANN001 - la cola de escrituras no importa aquí
        pass


def wait_threads(store: DataStore) -> None:
    for t in list(store._threads.values()):
        t.join(5)


def test_config_con_la_base_de_datos_dormida_no_espera_al_timeout():
    store = DataStore(SlowDB(delay=3.0, config={"demo.meaculpa": "0"}))
    t = time.monotonic()
    assert store.demo_enabled("meaculpa") is True  # valor por defecto mientras despierta
    assert time.monotonic() - t < 2.0  # como mucho FIRST_WAIT, no los 3 s de la conexión
    t = time.monotonic()
    store.demo_enabled("meaculpa")
    assert time.monotonic() - t < 0.2  # las siguientes ya no esperan
    wait_threads(store)
    assert store.demo_enabled("meaculpa") is False  # en cuanto responde, se usa lo de verdad


def test_config_con_la_base_de_datos_despierta_llega_en_la_primera_peticion():
    store = DataStore(SlowDB(delay=0.05, config={"modelo.gpt-4.1-mini": "0"}))
    assert store.model_enabled("gpt-4.1-mini") is False


def test_prefetch_lee_la_configuracion_sin_esperar():
    db = SlowDB(delay=0.3, config={"cuota.partidas": "7"})
    store = DataStore(db)
    t = time.monotonic()
    store.prefetch()
    assert time.monotonic() - t < 0.1
    assert store.quota("partidas", 30) == 7  # la primera consulta se suma a la lectura en marcha
    assert db.queries == 1


def test_bloqueo_de_usuario_sin_esperar_a_la_base_de_datos_dormida():
    db = SlowDB(delay=3.0)
    store = DataStore(db)
    t = time.monotonic()
    assert store.touch_user("a" * 64, "x@example.com", "X") is False  # sin respuesta aún: no bloqueado
    assert time.monotonic() - t < 2.0
    t = time.monotonic()
    store.touch_user("a" * 64, "x@example.com", "X")
    assert time.monotonic() - t < 0.2
    assert db.queries == 1  # una sola comprobación en marcha por usuario
    wait_threads(store)
    store._users["a" * 64] = (store._users["a" * 64][0], -1e9)  # caduca la caché
    assert store.touch_user("a" * 64, None, None) is True  # lo último conocido: bloqueado


def test_bloqueo_de_usuario_con_la_base_de_datos_despierta():
    store = DataStore(SlowDB(delay=0.05))
    assert store.touch_user("b" * 64, None, None) is True


def test_base_de_datos_caida_usa_valores_por_defecto():
    store = DataStore(SlowDB(delay=0.0, fail=True))
    assert store.quota("partidas", 30) == 30
    assert store.touch_user("c" * 64, None, None) is False
