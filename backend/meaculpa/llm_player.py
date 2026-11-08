"""Rival LLM: el modelo propone, python-chess valida.

Cada propuesta se valida con python-chess. Si es ilegal o no se puede leer, se le
devuelve el error al modelo y se reintenta (hasta `max_retries`). Si sigue fallando,
se juega una jugada legal de respaldo (la del rival "codicioso") y queda registrado.
Todo intento se guarda para medir la tasa de jugadas ilegales por modelo y temperatura.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import chess

from .engines import GreedyOpponent, MoveDecision
from .llm.base import LLMProvider
from .prompts import move_feedback, move_messages
from .schemas import MoveProposal


@dataclass
class Attempt:
    propuesta: str | None
    valida: bool
    error: str | None
    latency_ms: float
    input_tokens: int
    output_tokens: int


@dataclass
class LLMMoveRecord:
    model: str
    provider: str
    temperature: float
    fen: str
    attempts: list[Attempt] = field(default_factory=list)
    fallback: bool = False
    final_uci: str = ""

    @property
    def illegal_count(self) -> int:
        return sum(1 for a in self.attempts if not a.valida)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["illegal_count"] = self.illegal_count
        return d


def parse_proposal(board: chess.Board, text: str | None) -> tuple[chess.Move | None, str | None]:
    """Devuelve (jugada, None) si es legal, o (None, motivo) si no."""
    if not text or not text.strip():
        return None, "respuesta vacía o que no cumple el formato"
    candidate = text.strip().split()[0].rstrip(".,;!")
    # 1) SAN
    try:
        return board.parse_san(candidate), None
    except chess.IllegalMoveError:
        return None, f"'{candidate}' es ilegal en esta posición"
    except chess.AmbiguousMoveError:
        return None, f"'{candidate}' es ambigua: indica la columna o fila de origen"
    except ValueError:
        pass
    # 2) UCI como segunda oportunidad (e2e4)
    try:
        move = chess.Move.from_uci(candidate.lower())
    except ValueError:
        return None, f"'{candidate}' no es notación SAN válida"
    if move in board.legal_moves:
        return move, None
    return None, f"'{candidate}' es ilegal en esta posición"


class LLMOpponent:
    id = "llm"

    def __init__(
        self,
        provider: LLMProvider,
        temperature: float = 0.7,
        max_retries: int = 3,
        show_legal_moves: bool = False,
        metrics: MetricsStore | None = None,
    ) -> None:
        self.provider = provider
        self.temperature = temperature
        self.max_retries = max_retries
        self.show_legal_moves = show_legal_moves
        self.metrics = metrics
        self._fallback = GreedyOpponent()

    def choose_move(self, board: chess.Board) -> MoveDecision:
        record = self.play(board)
        return MoveDecision(chess.Move.from_uci(record.final_uci), {"engine": "llm", "registro": record.to_dict()})

    def play(self, board: chess.Board) -> LLMMoveRecord:
        history = _history_san(board)
        messages = move_messages(board, history, self.show_legal_moves)
        record = LLMMoveRecord(
            model=self.provider.name, provider=self.provider.kind, temperature=self.temperature, fen=board.fen()
        )
        move: chess.Move | None = None
        for _ in range(1 + self.max_retries):
            res = self.provider.structured(messages, MoveProposal, self.temperature)
            proposal = res.parsed.jugada if res.parsed else None
            if res.parsed is None:
                move, error = None, f"respuesta no parseable ({res.error or 'sin detalle'})"
            else:
                move, error = parse_proposal(board, proposal)
            record.attempts.append(
                Attempt(proposal, move is not None, error, res.latency_ms, res.input_tokens, res.output_tokens)
            )
            if move is not None:
                break
            messages = [
                *messages,
                {"role": "assistant", "content": res.raw_text or (proposal or "")},
                {"role": "user", "content": move_feedback(proposal or "", error or "inválida")},
            ]
        if move is None:
            move = self._fallback.choose_move(board).move
            record.fallback = True
        record.final_uci = move.uci()
        if self.metrics:
            self.metrics.append(record)
        return record


def _history_san(board: chess.Board) -> list[str]:
    replay = chess.Board(board.root().fen()) if board.move_stack else board.copy()
    out = []
    for m in board.move_stack:
        out.append(replay.san(m))
        replay.push(m)
    return out


class MetricsStore:
    """Registro JSONL de cada jugada del LLM + agregados por (modelo, temperatura)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()

    def append(self, record: LLMMoveRecord) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        row = record.to_dict()
        row["ts"] = time.time()
        with self._lock, self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    def rows(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        with self.path.open(encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def summary(self) -> list[dict[str, Any]]:
        groups: dict[tuple[str, float], list[dict[str, Any]]] = {}
        for r in self.rows():
            groups.setdefault((r["model"], round(float(r["temperature"]), 2)), []).append(r)
        out = []
        for (model, temp), rows in sorted(groups.items()):
            proposals = sum(len(r["attempts"]) for r in rows)
            illegal = sum(r["illegal_count"] for r in rows)
            first_ok = sum(1 for r in rows if r["attempts"] and r["attempts"][0]["valida"])
            latencies = [a["latency_ms"] for r in rows for a in r["attempts"]]
            out.append(
                {
                    "modelo": model,
                    "temperatura": temp,
                    "jugadas": len(rows),
                    "propuestas": proposals,
                    "propuestas_ilegales": illegal,
                    "tasa_ilegal": round(illegal / proposals, 3) if proposals else 0.0,
                    "legal_a_la_primera": round(first_ok / len(rows), 3),
                    "fallbacks": sum(1 for r in rows if r["fallback"]),
                    "latencia_media_ms": round(sum(latencies) / len(latencies), 1) if latencies else 0.0,
                }
            )
        return out
