"""La barra de evaluación no puede servir de pista gratis en una partida que cuenta para el ranking.

/eval acepta cualquier FEN. Sin este control bastaría con pedir la evaluación de la posición actual más cada
jugada candidata para saber cuál es mejor sin que la partida pase a «con ayuda». La regla:

- Mientras la persona tenga una partida en curso sin ayudas (las que cuentan para el ranking), /eval solo evalúa
  posiciones de su historial: la actual y las anteriores. Cualquier otra posición se rechaza.
- Las posiciones de la partida que se indica en `game_id` (en curso o terminada) se evalúan siempre: es repasarla.
- Sin partidas que cuenten en curso, /eval evalúa cualquier posición.

Para no leer Blob en cada petición se guarda en memoria, por persona, el historial de las partidas que pasan por
la API (cada lectura o guardado lo actualiza) y, como mucho una vez por `ttl_s`, se repasan sus últimas `scan`
partidas por si alguna se ha jugado en otra réplica. Antes de rechazar se releen las partidas implicadas.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

import chess

from .games import GameRecord, GameStore

BLOCKED_MESSAGE = (
    "Tienes una partida en curso que cuenta para el ranking: mientras dure, la barra solo evalúa posiciones "
    "de esa partida. Termínala o pide una pista si quieres ayuda."
)


class EvalBlocked(Exception):
    """La posición no es de la partida en curso que cuenta para el ranking."""


def position_key(board: chess.Board) -> str:
    """Posición sin relojes de jugadas: piezas, turno, enroques y captura al paso (solo si es legal)."""
    return " ".join(board.fen(en_passant="legal").split()[:4])


def counts_for_ranking(game: GameRecord) -> bool:
    return game.status == "en_curso" and not game.con_ayuda


@dataclass(frozen=True)
class _Entry:
    keys: frozenset[str]
    active: bool  # en curso y sin ayudas


def _entry(game: GameRecord) -> _Entry:
    board = chess.Board()
    keys = {position_key(board)}
    for uci in game.moves:
        board.push_uci(uci)
        keys.add(position_key(board))
    return _Entry(frozenset(keys), counts_for_ranking(game))


class EvalGuard:
    MAX_OWNERS = 5000
    MAX_GAMES_PER_OWNER = 12

    def __init__(
        self, store: GameStore, *, scan: int = 6, ttl_s: float = 60.0, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.store = store
        self.scan = scan
        self.ttl_s = ttl_s
        self.clock = clock
        self._lock = threading.Lock()
        self._games: dict[str, dict[str, _Entry]] = {}
        self._scanned: dict[str, float] = {}

    def note(self, game: GameRecord) -> None:
        """La partida tal como está ahora (tras leerla o guardarla)."""
        if not game.owner:
            return
        entry = _entry(game)
        with self._lock:
            if len(self._games) > self.MAX_OWNERS:
                self._games.clear()
                self._scanned.clear()
            games = self._games.setdefault(game.owner, {})
            games.pop(game.id, None)
            games[game.id] = entry
            while len(games) > self.MAX_GAMES_PER_OWNER:
                # Se descarta la más antigua que no cuente; si todas cuentan, la más antigua.
                old = next((gid for gid, e in games.items() if not e.active), next(iter(games)))
                games.pop(old)

    def _reload(self, owner: str, game_id: str) -> None:
        game = self.store.get(owner, game_id)
        if game is not None:
            self.note(game)
        else:
            with self._lock:
                self._games.get(owner, {}).pop(game_id, None)

    def _rescan(self, owner: str, force: bool = False) -> None:
        now = self.clock()
        with self._lock:
            last = self._scanned.get(owner)
            if not force and last is not None and now - last < self.ttl_s:
                return
            self._scanned[owner] = now
        for game in self.store.recent(owner, limit=self.scan):
            self.note(game)

    def _allowed(self, owner: str, key: str, game_id: str | None) -> bool:
        with self._lock:
            games = dict(self._games.get(owner, {}))
        if game_id and game_id in games and key in games[game_id].keys:
            return True
        active = [e for e in games.values() if e.active]
        return not active or any(key in e.keys for e in active)

    def check(self, owner: str, board: chess.Board, game_id: str | None = None) -> None:
        """EvalBlocked si la posición no se puede evaluar ahora."""
        key = position_key(board)
        self._rescan(owner)
        if game_id:
            with self._lock:
                known = game_id in self._games.get(owner, {})
            if not known:
                self._reload(owner, game_id)
        if self._allowed(owner, key, game_id):
            return
        # Antes de rechazar, se releen las partidas implicadas por si han cambiado en otra réplica.
        with self._lock:
            ids = [gid for gid, e in self._games.get(owner, {}).items() if e.active]
        for gid in {*ids, *([game_id] if game_id else [])}:
            self._reload(owner, gid)
        if not self._allowed(owner, key, game_id):
            raise EvalBlocked(BLOCKED_MESSAGE)
