"""Rival LLM: el modelo propone, python-chess valida.

Cada propuesta se valida con python-chess. Si es ilegal o no se puede leer, se le devuelve el error al
modelo y se reintenta (hasta `max_retries`). Si sigue fallando:
- en la demo, se juega una jugada legal de respaldo (la del rival «codicioso») y queda registrado;
- en modo experimento (`elo_mode=True`), NO se juega nada: la partida se pierde por ilegalidad
  (`LLMForfeit`), como en Kaggle Game Arena o LLM Chess. Así el Elo mide al modelo, no a un híbrido.

La jugada se pide en salida estructurada (JSON con un campo `move`) si el proveedor la admite, y si no,
en texto: se extrae una jugada SAN con una expresión regular y se valida igual con python-chess.

Por jugada se registra: intentos, ilegales, tokens de entrada/salida, latencia y coste.
"""

from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

import chess

from .engines import GreedyOpponent, MoveDecision
from .llm.base import LLMProvider, cost_usd
from .llm.openai_compat import StructuredOutputUnsupported
from .prompts import move_feedback, move_messages
from .schemas import MoveProposal

OutputMode = Literal["auto", "json", "text"]


@dataclass
class Attempt:
    propuesta: str | None
    valida: bool
    error: str | None
    latency_ms: float
    input_tokens: int
    output_tokens: int
    cost_usd: float = 0.0
    modo: str = "json"
    respuesta: str | None = None  # primeros caracteres de la respuesta, para diagnosticar


@dataclass
class LLMMoveRecord:
    model: str
    provider: str
    temperature: float | None
    fen: str
    attempts: list[Attempt] = field(default_factory=list)
    fallback: bool = False
    forfeit: bool = False  # modo experimento: reintentos agotados → derrota por ilegalidad
    final_uci: str = ""

    @property
    def illegal_count(self) -> int:
        return sum(1 for a in self.attempts if not a.valida)

    @property
    def input_tokens(self) -> int:
        return sum(a.input_tokens for a in self.attempts)

    @property
    def output_tokens(self) -> int:
        return sum(a.output_tokens for a in self.attempts)

    @property
    def latency_ms(self) -> float:
        return sum(a.latency_ms for a in self.attempts)

    @property
    def cost_usd(self) -> float:
        return sum(a.cost_usd for a in self.attempts)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["intentos"] = len(self.attempts)
        d["illegal_count"] = self.illegal_count
        d["input_tokens"] = self.input_tokens
        d["output_tokens"] = self.output_tokens
        d["latency_ms"] = round(self.latency_ms, 1)
        d["cost_usd"] = round(self.cost_usd, 8)
        return d


class LLMForfeit(Exception):
    """Modo experimento: el modelo agotó los reintentos sin dar una jugada legal."""

    def __init__(self, record: LLMMoveRecord) -> None:
        super().__init__(f"{record.model}: {record.illegal_count} propuestas inválidas en {record.fen}")
        self.record = record


class BudgetExceeded(RuntimeError):
    pass


class CostTracker:
    """Gasto en USD con tope duro.

    Antes de cada llamada se RESERVA el peor caso (tokens de entrada estimados + max_tokens de salida);
    si gasto + reservas + peor caso pasa del tope, se lanza BudgetExceeded y la llamada no se hace.
    Al volver, la reserva se cambia por el coste real. Con `parent`, todo se suma también al padre
    (un contador por jugador que alimenta el contador global del experimento).
    """

    def __init__(self, limit_usd: float | None = None, spent_usd: float = 0.0, parent: CostTracker | None = None):
        self.limit_usd = limit_usd
        self.spent_usd = spent_usd
        self.pending_usd = 0.0
        self.parent = parent
        self._lock = threading.Lock()

    @property
    def exhausted(self) -> bool:
        # Con tope 0 solo pueden jugar modelos gratis (las reservas de los de pago ya fallan).
        return self.limit_usd is not None and self.limit_usd > 0 and self.spent_usd >= self.limit_usd

    def reserve(self, usd: float) -> float:
        if self.parent is not None:
            self.parent.reserve(usd)
        with self._lock:
            if self.limit_usd is not None and self.spent_usd + self.pending_usd + usd > self.limit_usd:
                if self.parent is not None:
                    self.parent.release(usd, 0.0)
                raise BudgetExceeded(
                    f"gasto {self.spent_usd:.4f} USD + reservado {self.pending_usd:.4f} + peor caso {usd:.4f} "
                    f"> tope {self.limit_usd:.4f} USD"
                )
            self.pending_usd += usd
        return usd

    def release(self, reserved: float, actual: float) -> None:
        with self._lock:
            self.pending_usd = max(0.0, self.pending_usd - reserved)
            self.spent_usd += actual
        if self.parent is not None:
            self.parent.release(reserved, actual)

    def add(self, usd: float) -> None:
        self.release(0.0, usd)

    def check(self) -> None:
        if self.exhausted:
            raise BudgetExceeded(f"gasto {self.spent_usd:.4f} USD ≥ tope {self.limit_usd:.4f} USD")


DEFAULT_OUTPUT_TOKENS_ESTIMATE = 4096


def worst_case_cost(provider: object, messages: list[dict[str, Any]]) -> float:
    """Peor caso de una llamada: ~3 caracteres por token de entrada y max_tokens de salida."""
    chars = sum(len(str(m.get("content") or "")) + len(json.dumps(m.get("native") or "")) for m in messages)
    out_tokens = getattr(provider, "max_tokens", None) or DEFAULT_OUTPUT_TOKENS_ESTIMATE
    return cost_usd(provider, chars // 3 + 50, int(out_tokens))


# ------------------------------------------------------------ jugada en texto
_SAN = r"(?:O-O-O|O-O|0-0-0|0-0|[KQRBN][a-h]?[1-8]?x?[a-h][1-8]|[a-h]x[a-h][1-8](?:=?[QRBN])?|[a-h][1-8](?:=?[QRBN])?)[+#]?"
_SAN_RE = re.compile(rf"(?<![A-Za-z0-9]){_SAN}(?![A-Za-z0-9])")
_LABEL_RE = re.compile(r"(?:jugada|move)\s*[:=]\s*\**\s*([^\n]*)", re.IGNORECASE)
_JSON_MOVE_RE = re.compile(r'"move"\s*:\s*"([^"]{1,20})"')
_MOVE_NUMBER_RE = re.compile(r"^\s*\d+\s*\.+\s*")


def extract_move_text(text: str | None, strict: bool = False) -> str | None:
    """Saca una jugada de una respuesta en texto libre.

    Orden: línea «Jugada: X» (la última) → JSON {"move": "X"} → la última jugada SAN que aparezca
    (esto último no con `strict`, p. ej. si la respuesta se cortó a medias por max_tokens).
    No valida: eso lo hace `parse_proposal` con python-chess.
    """
    if not text or not text.strip():
        return None
    labels = _LABEL_RE.findall(text)
    if labels:
        line = _MOVE_NUMBER_RE.sub("", labels[-1].strip().strip("*`"))
        m = _SAN_RE.search(line)
        if m:
            return m.group(0).replace("0", "O") if m.group(0).startswith("0") else m.group(0)
        token = line.split()[0] if line.split() else ""
        return token.strip("*`.,;") or None
    m = _JSON_MOVE_RE.search(text)
    if m:
        return m.group(1).strip()
    if strict:
        return None
    found = _SAN_RE.findall(text)
    if found:
        last = found[-1]
        return last.replace("0", "O") if last.startswith("0") else last
    return None


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
        temperature: float | None = None,
        max_retries: int = 3,
        show_legal_moves: bool = False,
        metrics: MetricsStore | None = None,
        elo_mode: bool = False,
        budget: CostTracker | None = None,
        output_mode: OutputMode = "auto",
    ) -> None:
        """`temperature=None` → no se envía (valor por defecto del proveedor)."""
        self.provider = provider
        self.temperature = temperature
        self.max_retries = max_retries
        self.show_legal_moves = show_legal_moves
        self.metrics = metrics
        self.elo_mode = elo_mode
        self.budget = budget
        self.output_mode = output_mode
        self._fallback = GreedyOpponent()
        self.metrics_context: dict[str, Any] = {}  # se añade a cada fila de métricas (p. ej. id de partida)

    def choose_move(self, board: chess.Board) -> MoveDecision:
        record = self.play(board)
        if record.forfeit:
            raise LLMForfeit(record)
        return MoveDecision(chess.Move.from_uci(record.final_uci), {"engine": "llm", "registro": record.to_dict()})

    def _structured(self) -> bool:
        if self.output_mode == "json":
            return True
        if self.output_mode == "text":
            return False
        return bool(getattr(self.provider, "supports_structured", True))

    def _ask(self, messages: list[dict[str, Any]], structured: bool):
        if structured:
            return self.provider.structured(messages, MoveProposal, self.temperature)
        return self.provider.text(messages, self.temperature)  # type: ignore[attr-defined]

    def _ask_within_budget(
        self, board: chess.Board, history: list[str], messages: list[dict[str, Any]], structured: bool
    ) -> tuple[Any, list[dict[str, Any]], bool]:
        """Una llamada al modelo con el peor caso reservado en el presupuesto (si lo hay)."""
        reserved = self.budget.reserve(worst_case_cost(self.provider, messages)) if self.budget else 0.0
        actual = 0.0
        try:
            try:
                res = self._ask(messages, structured)
            except StructuredOutputUnsupported:
                # El servidor no admite response_format: se rehace la petición en texto (no cuenta como intento).
                structured = False
                messages = move_messages(board, history, self.show_legal_moves, structured=False)
                res = self._ask(messages, structured)
            actual = cost_usd(self.provider, res.input_tokens, res.output_tokens)
        finally:
            # Si la llamada falla (error de API) no hay uso que contar: se libera la reserva sin coste.
            if self.budget:
                self.budget.release(reserved, actual)
        return res, messages, structured

    def play(self, board: chess.Board) -> LLMMoveRecord:
        history = _history_san(board)
        structured = self._structured()
        messages: list[dict[str, Any]] = move_messages(board, history, self.show_legal_moves, structured)
        record = LLMMoveRecord(
            model=self.provider.name, provider=self.provider.kind, temperature=self.temperature, fen=board.fen()
        )
        move: chess.Move | None = None
        for _ in range(1 + self.max_retries):
            res, messages, structured = self._ask_within_budget(board, history, messages, structured)
            cost = cost_usd(self.provider, res.input_tokens, res.output_tokens)
            if structured:
                proposal = res.parsed.move if res.parsed is not None else None  # type: ignore[attr-defined]
                if res.parsed is None:
                    move, error = None, f"respuesta no parseable ({res.error or 'sin detalle'})"
                else:
                    move, error = parse_proposal(board, proposal)
            else:
                proposal = extract_move_text(res.raw_text, strict=bool(res.error))
                if proposal is None:
                    detail = f": {res.error}" if res.error else ""
                    move, error = None, f"no se encontró ninguna jugada en la respuesta{detail}"
                else:
                    move, error = parse_proposal(board, proposal)
            record.attempts.append(
                Attempt(
                    proposal,
                    move is not None,
                    error,
                    res.latency_ms,
                    res.input_tokens,
                    res.output_tokens,
                    round(cost, 8),
                    "json" if structured else "texto",
                    (res.raw_text or "")[:300] or None,
                )
            )
            if move is not None:
                break
            assistant: dict[str, Any] = {"role": "assistant", "content": res.raw_text or (proposal or "")}
            if res.native is not None:
                assistant["native"] = res.native
            messages = [
                *messages,
                assistant,
                {"role": "user", "content": move_feedback(proposal or "", error or "inválida", structured)},
            ]
        if move is None:
            if self.elo_mode:
                record.forfeit = True
            else:
                move = self._fallback.choose_move(board).move
                record.fallback = True
        record.final_uci = move.uci() if move is not None else ""
        if self.metrics:
            self.metrics.append(record, **self.metrics_context)
        return record


def _history_san(board: chess.Board) -> list[str]:
    replay = chess.Board(board.root().fen()) if board.move_stack else board.copy()
    out = []
    for m in board.move_stack:
        out.append(replay.san(m))
        replay.push(m)
    return out


def _temp_key(value: Any) -> float | str:
    return "defecto" if value is None else round(float(value), 2)


class MetricsStore:
    """Registro JSONL de cada jugada del LLM + agregados por (modelo, temperatura)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()

    def append(self, record: LLMMoveRecord, **extra: Any) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        row = record.to_dict()
        row.update(extra)
        row["ts"] = time.time()
        with self._lock, self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    def rows(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        with self.path.open(encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def summary(self) -> list[dict[str, Any]]:
        groups: dict[tuple[str, float | str], list[dict[str, Any]]] = {}
        for r in self.rows():
            groups.setdefault((r["model"], _temp_key(r.get("temperature"))), []).append(r)
        out = []
        for (model, temp), rows in sorted(groups.items(), key=lambda kv: (kv[0][0], str(kv[0][1]))):
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
                    "fallbacks": sum(1 for r in rows if r.get("fallback")),
                    "derrotas_por_ilegal": sum(1 for r in rows if r.get("forfeit")),
                    "latencia_media_ms": round(sum(latencies) / len(latencies), 1) if latencies else 0.0,
                    "coste_usd": round(sum(float(r.get("cost_usd", 0.0)) for r in rows), 6),
                }
            )
        return out
