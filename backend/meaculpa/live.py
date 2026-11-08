"""Motor en vivo: la barra de evaluación (POST /eval) y las pistas.

Stockfish se arranca una vez y se reutiliza: un pool pequeño de procesos UCI, cada uno usado por un solo hilo a la
vez. Si un proceso no responde a tiempo o se cae, se cierra (no quedan procesos huérfanos) y el siguiente uso abre
otro. Sin Stockfish instalado (local y tests) se usa el alfa-beta propio a poca profundidad.

Todas las puntuaciones salen desde el punto de vista de las BLANCAS: `cp` en centipeones o `mate` en jugadas
(positivo = mate a favor de las blancas). Una posición en la que ya hay jaque mate da `mate = 0` y `resultado`.
"""

from __future__ import annotations

import atexit
import logging
import queue
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

import chess
import chess.engine

from .engines import AlphaBetaSearcher, find_stockfish, is_mate_score, mate_in_moves
from .games import TERMINATIONS

log = logging.getLogger("meaculpa.live")

MAX_PV = 6


class EngineBusy(Exception):
    """Todos los procesos del motor están ocupados."""


class EngineFailure(Exception):
    """El motor no ha respondido a tiempo o se ha caído."""


@dataclass
class Line:
    """Una línea del motor. `cp`/`mate` desde las blancas."""

    cp: int | None
    mate: int | None
    pv: list[chess.Move] = field(default_factory=list)
    depth: int = 0

    def score(self) -> dict[str, int | None]:
        return {"cp": self.cp, "mate": self.mate}

    def user_cp(self, color: chess.Color, mate_cp: int = 10_000) -> int:
        """Valoración en centipeones desde `color` (los mates como ±mate_cp, más cerca cuanto más corto)."""
        if self.mate is not None:
            value = mate_cp - abs(self.mate) if self.mate != 0 else mate_cp
            value = value if self.mate > 0 else -value
        else:
            value = self.cp or 0
        return value if color == chess.WHITE else -value


def _white_line(info: dict[str, Any]) -> Line:
    score = info["score"].white()
    pv = list(info.get("pv") or [])
    return Line(cp=score.score(), mate=score.mate(), pv=pv, depth=int(info.get("depth") or 0))


def pv_san(board: chess.Board, pv: list[chess.Move], limit: int = MAX_PV) -> list[str]:
    b = board.copy(stack=False)
    out: list[str] = []
    for move in pv[:limit]:
        if move not in b.legal_moves:
            break
        out.append(b.san(move))
        b.push(move)
    return out


def terminal_payload(board: chess.Board) -> dict[str, Any] | None:
    """Posición terminada: el resultado sin pedir nada al motor. None si se puede seguir jugando."""
    outcome = board.outcome(claim_draw=True)
    if outcome is None:
        return None
    mated = outcome.termination == chess.Termination.CHECKMATE
    return {
        "cp": None if mated else 0,
        "mate": 0 if mated else None,
        "best_uci": None,
        "best_san": None,
        "pv_san": [],
        "depth": 0,
        "resultado": outcome.result(),
        "fin": TERMINATIONS.get(outcome.termination, outcome.termination.name.lower()),
    }


class _Slot:
    def __init__(self) -> None:
        self.engine: chess.engine.SimpleEngine | None = None


class LiveEngine:
    """Pool de Stockfish seguro con hilos. `analyse` devuelve las `multipv` mejores líneas."""

    def __init__(
        self,
        path: str | None = None,
        *,
        size: int = 2,
        timeout_s: float = 5.0,
        acquire_timeout_s: float = 3.0,
        fallback_depth: int = 3,
    ) -> None:
        self.path = path if path is not None else find_stockfish()
        self.timeout_s = timeout_s
        self.acquire_timeout_s = acquire_timeout_s
        self.fallback_depth = fallback_depth
        self._slots: queue.LifoQueue[_Slot] = queue.LifoQueue()
        for _ in range(max(1, size)):
            self._slots.put(_Slot())
        self._all: list[_Slot] = []
        self._lock = threading.Lock()
        self._closed = False
        atexit.register(self.close)

    @property
    def name(self) -> str:
        return "Stockfish" if self.path else f"alfa-beta propio (profundidad {self.fallback_depth})"

    @contextmanager
    def _slot(self) -> Iterator[_Slot]:
        if self._closed:
            raise EngineFailure("motor cerrado")
        try:
            slot = self._slots.get(timeout=self.acquire_timeout_s)
        except queue.Empty as exc:
            raise EngineBusy() from exc
        try:
            yield slot
        finally:
            self._slots.put(slot)

    def _open(self, slot: _Slot) -> chess.engine.SimpleEngine:
        if slot.engine is None:
            assert self.path
            engine = chess.engine.SimpleEngine.popen_uci(self.path, timeout=self.timeout_s)
            try:
                engine.configure({"Threads": 1, "Hash": 16})
            except Exception:  # noqa: BLE001 - opciones no soportadas: se sigue con las de fábrica
                log.warning("Stockfish no acepta Threads/Hash; se usan sus valores por defecto")
            slot.engine = engine
            with self._lock:
                if slot not in self._all:
                    self._all.append(slot)
        return slot.engine

    @staticmethod
    def _kill(slot: _Slot) -> None:
        engine, slot.engine = slot.engine, None
        if engine is not None:
            try:
                engine.close()  # termina el proceso aunque no responda
            except Exception:  # noqa: BLE001
                log.exception("No se pudo cerrar un proceso de Stockfish")

    def analyse(
        self, board: chess.Board, *, time_s: float = 0.2, depth: int | None = None, multipv: int = 1
    ) -> list[Line]:
        """Las mejores líneas de una posición que no ha terminado. EngineBusy / EngineFailure si no se puede."""
        if board.is_game_over():
            return []
        if not self.path:
            return self._fallback(board)
        limit = chess.engine.Limit(time=time_s, depth=depth)
        with self._slot() as slot:
            try:
                engine = self._open(slot)
                infos = engine.analyse(board, limit, multipv=max(1, multipv))
            except Exception as exc:  # noqa: BLE001 - tiempo agotado, proceso caído o error del protocolo
                log.warning("Stockfish no ha respondido (%s); se reinicia el proceso", type(exc).__name__)
                self._kill(slot)
                raise EngineFailure(str(exc) or type(exc).__name__) from exc
        lines = [_white_line(info) for info in (infos if isinstance(infos, list) else [infos]) if "score" in info]
        if not lines:
            raise EngineFailure("Stockfish no ha devuelto valoración")
        return lines

    def _fallback(self, board: chess.Board) -> list[Line]:
        with self._slot():
            res = AlphaBetaSearcher(depth=self.fallback_depth).search(board)
        sign = 1 if board.turn == chess.WHITE else -1
        if is_mate_score(res.score):
            line = Line(cp=None, mate=sign * mate_in_moves(res.score))
        else:
            line = Line(cp=sign * res.score, mate=None)
        line.pv = list(res.pv) or ([res.move] if res.move else [])
        line.depth = res.depth
        return [line]

    def close(self) -> None:
        self._closed = True
        with self._lock:
            slots = list(self._all)
        for slot in slots:
            self._kill(slot)


def eval_payload(board: chess.Board, lines: list[Line]) -> dict[str, Any]:
    """Respuesta de POST /eval a partir de la mejor línea.

    Solo la valoración: la mejor jugada y la línea del motor van siempre vacías. /eval se pide sobre la posición
    actual de partidas que cuentan para el ranking, y devolver la jugada de Stockfish ahí sería una pista gratis que
    no marca la partida «con ayuda». La mejor jugada solo sale por /hint, que sí la marca.
    """
    best = lines[0]
    return {
        **best.score(),
        "best_uci": None,
        "best_san": None,
        "pv_san": [],
        "depth": best.depth,
        "resultado": None,
        "fin": None,
    }


class EvalRateLimiter:
    """Límite por persona de la barra de evaluación, en memoria (no toca Blob ni la base de datos).

    Cubo de fichas: de media una petición cada `interval_s`, con ráfagas de hasta `burst`; y un tope diario.
    """

    def __init__(self, interval_s: float, per_day: int, burst: int = 8, clock=time.monotonic, wall=time.time) -> None:
        self.rate = 1.0 / max(interval_s, 1e-3)
        self.burst = float(burst)
        self.per_day = per_day
        self.clock = clock
        self.wall = wall
        self._lock = threading.Lock()
        self._buckets: dict[str, tuple[float, float]] = {}
        self._day = ""
        self._days: dict[str, int] = {}

    def allow(self, owner: str) -> str | None:
        """None si se permite; si no, el motivo ("rafaga" o "diario")."""
        now = self.clock()
        day = time.strftime("%Y-%m-%d", time.gmtime(self.wall()))
        with self._lock:
            if len(self._buckets) > 20_000:
                self._buckets.clear()
            if day != self._day:
                self._day, self._days = day, {}
            tokens, last = self._buckets.get(owner, (self.burst, now))
            tokens = min(self.burst, tokens + (now - last) * self.rate)
            if tokens < 1.0:
                self._buckets[owner] = (tokens, now)
                return "rafaga"
            used = self._days.get(owner, 0)
            if used >= self.per_day:
                return "diario"
            self._buckets[owner] = (tokens - 1.0, now)
            self._days[owner] = used + 1
            return None
