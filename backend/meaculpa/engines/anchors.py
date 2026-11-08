"""Stockfish como ancla de Elo para los experimentos.

- Un proceso persistente por partida (`chess.engine.SimpleEngine`), no uno por jugada.
- `Threads=1` y `Hash` pequeño: resultados reproducibles y sin pelear por la CPU.
- `UCI_LimitStrength=true` + `UCI_Elo` (mínimo 1320 en Stockfish 16+). La escala de UCI_Elo está
  calibrada en CCRL Blitz a 120 s + 1 s; con un límite por nodos o por tiempo fijo por jugada el Elo real
  del ancla puede variar, así que conviene anotar el límite junto al resultado.
- Límite constante por jugada: por nodos (`nodes`) o por tiempo (`movetime`, segundos).
"""

from __future__ import annotations

import contextlib
from typing import Any

import chess
import chess.engine

from .opponents import MoveDecision, find_stockfish

UCI_ELO_MIN = 1320


class StockfishAnchor:
    id = "stockfish"

    def __init__(
        self,
        path: str | None = None,
        *,
        uci_elo: int | None = 1320,
        nodes: int | None = None,
        movetime: float | None = None,
        threads: int = 1,
        hash_mb: int = 16,
        engine_factory=None,
    ) -> None:
        """`uci_elo=None` → fuerza completa. Sin `nodes` ni `movetime`, 0,1 s por jugada.

        `engine_factory(path)` solo se inyecta en tests (por defecto `SimpleEngine.popen_uci`).
        """
        self.path = path or find_stockfish()
        if not self.path:
            raise RuntimeError("Stockfish no encontrado: define STOCKFISH_PATH o pon 'stockfish' en el PATH")
        if uci_elo is not None and uci_elo < UCI_ELO_MIN:
            raise ValueError(f"UCI_Elo mínimo de Stockfish: {UCI_ELO_MIN} (pedido {uci_elo})")
        self.uci_elo = uci_elo
        self.nodes = int(nodes) if nodes else None
        self.movetime = float(movetime) if movetime else (None if self.nodes else 0.1)
        self.threads = threads
        self.hash_mb = hash_mb
        self._factory = engine_factory or chess.engine.SimpleEngine.popen_uci
        self._engine: Any = None

    # ------------------------------------------------------------ ciclo de vida
    def options(self) -> dict[str, Any]:
        opts: dict[str, Any] = {"Threads": self.threads, "Hash": self.hash_mb}
        if self.uci_elo is not None:
            opts["UCI_LimitStrength"] = True
            opts["UCI_Elo"] = self.uci_elo
        return opts

    def limit(self) -> chess.engine.Limit:
        if self.nodes:
            return chess.engine.Limit(nodes=self.nodes)
        return chess.engine.Limit(time=self.movetime)

    def start(self) -> None:
        if self._engine is None:
            self._engine = self._factory(self.path)
            self._engine.configure(self.options())

    def new_game(self) -> None:
        """Abre el proceso si hace falta y limpia el hash entre partidas."""
        if self._engine is None:
            self.start()
        else:
            self.close()
            self.start()

    def close(self) -> None:
        if self._engine is not None:
            with contextlib.suppress(chess.engine.EngineError, chess.engine.EngineTerminatedError, OSError):
                self._engine.quit()
            self._engine = None

    def __enter__(self) -> StockfishAnchor:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------ jugar
    def choose_move(self, board: chess.Board) -> MoveDecision:
        if self._engine is None:
            self.start()
        result = self._engine.play(board, self.limit())
        if result.move is None:  # pragma: no cover - Stockfish siempre devuelve jugada si hay legales
            raise RuntimeError("Stockfish no devolvió jugada")
        return MoveDecision(result.move, {"engine": "stockfish", "uci_elo": self.uci_elo})

    def describe(self) -> dict[str, Any]:
        return {"uci_elo": self.uci_elo, "nodes": self.nodes, "movetime": self.movetime, "hash": self.hash_mb}
