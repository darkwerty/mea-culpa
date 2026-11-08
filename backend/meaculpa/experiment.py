"""Torneo de calibración: participantes (motores o LLM) contra anclas de Elo fijo.

Configuración en YAML o JSON (ver `backend/scripts/experiment.py` y `eval/experimentos/ejemplo.yaml`).
Reglas de cada partida:
- Arranca desde una posición tras 4–8 plies de una partida amateur real (`openings.py`), sin repetir
  dentro de un emparejamiento, alternando colores (cada posición con los dos colores si `ambos_colores`).
- Termina con `board.outcome(claim_draw=True)` o al llegar a `tope_plies` (300): tablas, anotadas aparte.
- LLM en modo experimento: agotar los reintentos es DERROTA por ilegalidad (no juega el codicioso).
- Un error de API o el tope de gasto dejan la partida como «*» (no cuenta para el Elo, se anota aparte).
- El tope de gasto en USD es duro: cada llamada reserva su peor caso antes de hacerse.

Salidas en `salida/`: `pgn/<id>.pgn` y `partidas.pgn` (todas), `partidas.jsonl` (una fila por partida),
`jugadas_llm.jsonl` (una fila por jugada del LLM: intentos, ilegales, tokens, latencia, coste) y
`resultados.md` (Elo por máxima verosimilitud contra las anclas, IC 95 % por bootstrap, métricas).
Si se relanza con la misma salida, se saltan las partidas ya terminadas y se suma el gasto previo.
"""

from __future__ import annotations

import json
import os
import random
import re
import statistics
import threading
import time
from collections import defaultdict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import chess
import chess.pgn

from .elo import EloEstimate, estimate
from .engines import GreedyOpponent, MinimaxOpponent, RandomOpponent, StockfishAnchor, find_stockfish
from .llm.base import LLMProvider
from .llm_player import BudgetExceeded, CostTracker, LLMForfeit, LLMMoveRecord, LLMOpponent, MetricsStore
from .openings import StartPosition, assign_openings, extract_start_positions, read_games, stockfish_evaluator

ENGINE_TYPES = ("aleatorio", "codicioso", "minimax", "stockfish")
LLM_TYPES = ("azure_openai", "claude_foundry", "ollama", "mock")
MAX_CONSECUTIVE_ERRORS = 3
ELO_LO, ELO_HI = -1000.0, 4000.0  # dominio de la estimación (mismo que elo.estimate)

# ----------------------------------------------------------------- configuración
_ENV_RE = re.compile(r"\$\{([A-Z0-9_]+)(?::-([^}]*))?\}")


def _expand(value: Any) -> Any:
    """${VAR} y ${VAR:-defecto} en cualquier texto de la configuración."""
    if isinstance(value, str):
        return _ENV_RE.sub(lambda m: os.environ.get(m.group(1), m.group(2) or ""), value)
    if isinstance(value, list):
        return [_expand(v) for v in value]
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    return value


def load_config(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() in (".yaml", ".yml"):
        import yaml

        raw = yaml.safe_load(text)
    else:
        raw = json.loads(text)
    cfg = _expand(raw or {})
    validate_config(cfg)
    return cfg


def validate_config(cfg: dict[str, Any]) -> None:
    names: set[str] = set()
    for section in ("anclas", "participantes"):
        for spec in cfg.get(section) or []:
            name = spec.get("nombre")
            if not name or not re.fullmatch(r"[A-Za-z0-9._\-]+", name):
                raise ValueError(f"{section}: nombre no válido {name!r} (letras, números, . _ -)")
            if name in names:
                raise ValueError(f"nombre repetido: {name}")
            names.add(name)
            tipo = spec.get("tipo")
            if tipo not in ENGINE_TYPES + LLM_TYPES:
                raise ValueError(f"{name}: tipo {tipo!r} desconocido; opciones: {', '.join(ENGINE_TYPES + LLM_TYPES)}")
    if not cfg.get("anclas"):
        raise ValueError("Hace falta al menos un ancla con Elo fijo")
    for spec in cfg["anclas"]:
        if spec.get("elo") is None:
            raise ValueError(f"El ancla {spec['nombre']} necesita 'elo'")
    if not cfg.get("participantes"):
        raise ValueError("Hace falta al menos un participante")
    if cfg.get("modo", "anclas") not in ("anclas", "todos"):
        raise ValueError("modo: anclas | todos")


# ------------------------------------------------------------------- jugadores
def build_provider(spec: dict[str, Any]) -> LLMProvider:
    tipo = spec["tipo"]
    prices = {
        "price_input_per_m": float(spec.get("precio_entrada_1m", 0.0)),
        "price_output_per_m": float(spec.get("precio_salida_1m", 0.0)),
    }
    if tipo == "azure_openai":
        from .llm.azure import AzureOpenAIProvider

        return AzureOpenAIProvider(
            spec.get("endpoint") or os.environ.get("AZURE_OPENAI_ENDPOINT", ""),
            spec["deployment"],
            api_version=spec.get("api_version") or None,
            api_key=spec.get("api_key") or os.environ.get("AZURE_OPENAI_API_KEY") or None,
            auth=spec.get("auth", "default"),
            name=spec["nombre"],
            reasoning_effort=spec.get("reasoning_effort"),
            max_tokens=spec.get("max_tokens"),
            supports_temperature=bool(spec.get("admite_temperatura", True)),
            structured_output=spec.get("salida_estructurada", "auto"),
            **prices,
        )
    if tipo == "claude_foundry":
        from .llm.claude import ClaudeFoundryProvider

        return ClaudeFoundryProvider(
            spec["deployment"],
            resource=spec.get("recurso") or os.environ.get("ANTHROPIC_FOUNDRY_RESOURCE") or None,
            base_url=spec.get("base_url") or os.environ.get("ANTHROPIC_FOUNDRY_BASE_URL") or None,
            api_key=spec.get("api_key") or os.environ.get("ANTHROPIC_FOUNDRY_API_KEY") or None,
            auth=spec.get("auth", "default"),
            name=spec["nombre"],
            max_tokens=spec.get("max_tokens"),
            effort=spec.get("effort"),
            thinking=spec.get("thinking"),
            **prices,
        )
    if tipo == "ollama":
        from .llm.ollama import OllamaProvider

        return OllamaProvider(
            spec["modelo"],
            base_url=spec.get("base_url"),
            name=spec["nombre"],
            reasoning_effort=spec.get("reasoning_effort"),
            max_tokens=spec.get("max_tokens"),
            structured_output=spec.get("salida_estructurada", "auto"),
            **prices,
        )
    if tipo == "mock":
        from .llm.mock import MockProvider

        return MockProvider(
            name=spec["nombre"],
            seed=spec.get("semilla"),
            illegal_base=float(spec.get("ilegal_base", 0.05)),
            **prices,
        )
    raise ValueError(f"{spec['nombre']}: {tipo} no es un proveedor LLM")


def describe_spec(spec: dict[str, Any]) -> str:
    """Resumen legible de la configuración de un jugador (sin claves)."""
    skip = {"nombre", "tipo", "api_key", "endpoint", "recurso", "base_url"}
    parts = [f"{k}={v}" for k, v in spec.items() if k not in skip and v is not None]
    return f"{spec['tipo']} ({', '.join(parts)})" if parts else spec["tipo"]


class Player:
    """Un jugador para UNA partida (los motores con estado se abren al empezar y se cierran al acabar)."""

    def __init__(self, spec: dict[str, Any], ctx: ExperimentContext, game_id: str) -> None:
        self.spec = spec
        self.name: str = spec["nombre"]
        self.kind: str = spec["tipo"]
        self.records: list[LLMMoveRecord] = []
        self.cost = CostTracker(parent=ctx.budget)
        seed = random.Random(f"{ctx.seed}:{game_id}:{self.name}").randrange(2**31)
        self._engine: Any = None
        self._llm: LLMOpponent | None = None
        if self.kind == "aleatorio":
            self._engine = RandomOpponent(seed=seed)
        elif self.kind == "codicioso":
            self._engine = GreedyOpponent(seed=seed)
        elif self.kind == "minimax":
            self._engine = MinimaxOpponent(depth=int(spec.get("profundidad", 2)))
        elif self.kind == "stockfish":
            self._engine = StockfishAnchor(
                spec.get("ruta") or ctx.stockfish_path,
                uci_elo=spec.get("uci_elo"),
                nodes=spec.get("nodos"),
                movetime=spec.get("tiempo"),
                hash_mb=int(spec.get("hash", 16)),
                engine_factory=ctx.engine_factory,
            )
            self._engine.start()  # un proceso por partida
        else:
            llm_cfg = {**ctx.llm_defaults, **spec}
            self._llm = LLMOpponent(
                ctx.provider(spec),
                temperature=llm_cfg.get("temperatura"),
                max_retries=int(llm_cfg.get("max_reintentos", 3)),
                show_legal_moves=bool(llm_cfg.get("mostrar_legales", False)),
                metrics=ctx.metrics,
                elo_mode=True,
                budget=self.cost,
                output_mode={"auto": "auto", "json": "json", "texto": "text", "text": "text"}[
                    str(llm_cfg.get("salida", "auto"))
                ],
            )
            self._llm.metrics_context = {"partida": game_id, "jugador": self.name}

    @property
    def is_llm(self) -> bool:
        return self._llm is not None

    def choose(self, board: chess.Board) -> chess.Move:
        if self._llm is not None:
            record = self._llm.play(board)
            self.records.append(record)
            if record.forfeit:
                raise LLMForfeit(record)
            return chess.Move.from_uci(record.final_uci)
        return self._engine.choose_move(board).move

    def close(self) -> None:
        if isinstance(self._engine, StockfishAnchor):
            self._engine.close()

    def stats(self) -> dict[str, Any] | None:
        if not self.is_llm:
            return None
        recs = self.records
        lat = sorted(a.latency_ms for r in recs for a in r.attempts)
        return {
            "jugadas": len(recs),
            "intentos": sum(len(r.attempts) for r in recs),
            "ilegales": sum(r.illegal_count for r in recs),
            "legal_a_la_primera": sum(1 for r in recs if r.attempts and r.attempts[0].valida),
            "tokens_entrada": sum(r.input_tokens for r in recs),
            "tokens_salida": sum(r.output_tokens for r in recs),
            "coste_usd": round(self.cost.spent_usd, 8),
            "latencias_ms": [round(x, 1) for x in lat],
        }


@dataclass
class ExperimentContext:
    seed: int
    budget: CostTracker
    metrics: MetricsStore
    llm_defaults: dict[str, Any]
    stockfish_path: str | None
    provider_factory: Callable[[dict[str, Any]], LLMProvider] = build_provider
    engine_factory: Any = None
    _providers: dict[str, LLMProvider] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def provider(self, spec: dict[str, Any]) -> LLMProvider:
        """Un proveedor por participante, compartido entre partidas (los clientes HTTP son reutilizables)."""
        with self._lock:
            if spec["nombre"] not in self._providers:
                self._providers[spec["nombre"]] = self.provider_factory(spec)
            return self._providers[spec["nombre"]]


# --------------------------------------------------------------------- partida
@dataclass
class ScheduledGame:
    id: str
    round: int
    a: dict[str, Any]  # «jugador» (el participante)
    b: dict[str, Any]  # «rival» (ancla u otro participante)
    opening: StartPosition
    a_white: bool


def play_game(game: ScheduledGame, ctx: ExperimentContext, max_plies: int) -> tuple[dict[str, Any], chess.Board]:
    pa = Player(game.a, ctx, game.id)
    pb = Player(game.b, ctx, game.id)
    white, black = (pa, pb) if game.a_white else (pb, pa)
    board = game.opening.board()
    plies = 0
    error = None
    t0 = time.perf_counter()
    try:
        while True:
            outcome = board.outcome(claim_draw=True)
            if outcome is not None:
                result, termination = outcome.result(), outcome.termination.name.lower()
                break
            if plies >= max_plies:
                result, termination = "1/2-1/2", "tope_plies"
                break
            mover = white if board.turn == chess.WHITE else black
            try:
                move = mover.choose(board)
            except LLMForfeit:
                result = "0-1" if board.turn == chess.WHITE else "1-0"
                termination = "ilegal"
                break
            except BudgetExceeded as exc:
                result, termination, error = "*", "presupuesto", str(exc)
                break
            except Exception as exc:  # errores de API: la partida no cuenta (nunca tablas en silencio)
                result, termination, error = "*", "error_api", f"{type(exc).__name__}: {exc}"[:500]
                break
            if move not in board.legal_moves:  # defensa en profundidad
                raise RuntimeError(f"{mover.name} devolvió una jugada ilegal: {move}")
            board.push(move)
            plies += 1
    finally:
        pa.close()
        pb.close()

    score_a = {"1-0": 1.0, "0-1": 0.0, "1/2-1/2": 0.5}.get(result)
    if score_a is not None and not game.a_white:
        score_a = 1.0 - score_a
    row = {
        "id": game.id,
        "ronda": game.round,
        "jugador": pa.name,
        "rival": pb.name,
        "blancas": white.name,
        "negras": black.name,
        "color_jugador": "blancas" if game.a_white else "negras",
        "resultado": result,
        "puntos_jugador": score_a,
        "terminacion": termination,
        "plies": plies,
        "apertura": game.opening.id,
        "apertura_plies": game.opening.plies,
        "apertura_origen": game.opening.origen,
        "llm": {p.name: p.stats() for p in (pa, pb) if p.is_llm},
        "coste_usd": round(pa.cost.spent_usd + pb.cost.spent_usd, 8),
        "duracion_s": round(time.perf_counter() - t0, 2),
        "error": error,
        "ts": time.time(),
    }
    return row, board


def to_pgn(row: dict[str, Any], board: chess.Board, event: str, elos: dict[str, float]) -> str:
    game = chess.pgn.Game.from_board(board)
    h = game.headers
    h["Event"] = event
    h["Site"] = "Mea culpa (local)"
    h["Date"] = time.strftime("%Y.%m.%d", time.localtime(row["ts"]))
    h["Round"] = str(row["ronda"])
    h["White"], h["Black"] = row["blancas"], row["negras"]
    h["Result"] = row["resultado"]
    h["Termination"] = row["terminacion"]
    for color, name in (("White", row["blancas"]), ("Black", row["negras"])):
        if name in elos:
            h[f"{color}Elo"] = str(int(round(elos[name])))
    h["MeaCulpaId"] = row["id"]
    h["MeaCulpaApertura"] = row["apertura"]
    h["MeaCulpaAperturaOrigen"] = row["apertura_origen"]
    h["MeaCulpaPlyInicio"] = str(row["apertura_plies"])
    node: chess.pgn.GameNode = game
    for _ in range(row["apertura_plies"]):
        if not node.variations:
            break
        node = node.variations[0]
    if node is not game:
        node.comment = "fin de la apertura tomada de una partida real; desde aquí juegan los participantes"
    exporter = chess.pgn.StringExporter(headers=True, variations=False, comments=True)
    return game.accept(exporter) + "\n\n"


# -------------------------------------------------------------------- calendario
def load_openings(cfg: dict[str, Any], needed: int, stockfish_path: str | None) -> list[StartPosition]:
    oc = cfg.get("aperturas") or {}
    pgn = oc.get("pgn")
    if not pgn:
        raise ValueError("aperturas.pgn: ruta al PGN de partidas reales")
    games = read_games(Path(pgn))
    evaluator, closer = None, None
    max_cp = oc.get("max_eval_cp")
    if max_cp is not None:
        if not stockfish_path:
            raise RuntimeError("aperturas.max_eval_cp necesita Stockfish (STOCKFISH_PATH)")
        evaluator, closer = stockfish_evaluator(stockfish_path, depth=int(oc.get("profundidad_eval", 12)))
    try:
        positions = extract_start_positions(
            games,
            min_plies=int(oc.get("plies_min", 4)),
            max_plies=int(oc.get("plies_max", 8)),
            limit=needed,
            seed=int(cfg.get("semilla", 0)),
            user=oc.get("usuario"),
            evaluator=evaluator,
            max_abs_cp=max_cp,
        )
    finally:
        if closer:
            closer()
    return positions


def schedule(cfg: dict[str, Any], positions: list[StartPosition]) -> list[ScheduledGame]:
    n = int(cfg.get("partidas_por_emparejamiento", 10))
    both = bool((cfg.get("aperturas") or {}).get("ambos_colores", True))
    participants, anchors = cfg["participantes"], cfg["anclas"]
    pairs = [(p, a) for p in participants for a in anchors]
    if cfg.get("modo", "anclas") == "todos":
        pairs += [(p, q) for i, p in enumerate(participants) for q in participants[i + 1 :]]
    plan = assign_openings(positions, n, both_colors=both)
    games = []
    # Intercalado: primero la partida 1 de cada emparejamiento, luego la 2... Si el presupuesto corta
    # el experimento, todos los emparejamientos quedan con un número parecido de partidas.
    for i, (opening, a_white) in enumerate(plan):
        for a, b in pairs:
            games.append(ScheduledGame(f"{a['nombre']}__{b['nombre']}__{i + 1:03d}", i + 1, a, b, opening, a_white))
    return games


# ------------------------------------------------------------------- ejecución
@dataclass
class ExperimentResult:
    rows: list[dict[str, Any]]
    estimates: dict[str, EloEstimate]
    spent_usd: float
    stopped: str | None
    report_path: Path


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def run_experiment(
    cfg: dict[str, Any],
    out_dir: Path,
    *,
    provider_factory: Callable[[dict[str, Any]], LLMProvider] = build_provider,
    engine_factory: Any = None,
    max_games: int | None = None,
    log: Callable[[str], None] = print,
) -> ExperimentResult:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "pgn").mkdir(exist_ok=True)
    games_path = out_dir / "partidas.jsonl"
    previous = _read_jsonl(games_path)
    done = {r["id"] for r in previous if r["resultado"] != "*"}
    spent_before = sum(float(r.get("coste_usd") or 0.0) for r in previous)
    budget_limit = cfg.get("presupuesto_usd")
    budget = CostTracker(float(budget_limit) if budget_limit is not None else None, spent_usd=spent_before)

    needs_sf = any(s["tipo"] == "stockfish" for s in cfg["anclas"] + cfg["participantes"])
    sf_path = cfg.get("stockfish") or find_stockfish()
    if needs_sf and not sf_path and engine_factory is None:
        raise RuntimeError("Hay anclas de Stockfish pero no se encuentra el binario: define STOCKFISH_PATH")

    n = int(cfg.get("partidas_por_emparejamiento", 10))
    both = bool((cfg.get("aperturas") or {}).get("ambos_colores", True))
    positions = load_openings(cfg, -(-n // 2) if both else n, sf_path)
    (out_dir / "aperturas.json").write_text(
        json.dumps([p.to_dict() for p in positions], ensure_ascii=False, indent=2), encoding="utf-8"
    )
    ctx = ExperimentContext(
        seed=int(cfg.get("semilla", 0)),
        budget=budget,
        metrics=MetricsStore(out_dir / "jugadas_llm.jsonl"),
        llm_defaults=dict(cfg.get("llm") or {}),
        stockfish_path=sf_path,
        provider_factory=provider_factory,
        engine_factory=engine_factory,
    )
    todo = [g for g in schedule(cfg, positions) if g.id not in done]
    if max_games is not None:
        todo = todo[:max_games]
    max_plies = int(cfg.get("tope_plies", 300))
    event = f"Mea culpa · {cfg.get('nombre', 'experimento')}"
    elos = {a["nombre"]: float(a["elo"]) for a in cfg["anclas"]}
    write_lock = threading.Lock()
    stop = threading.Event()
    stopped: list[str] = []
    consecutive_errors = [0]
    new_rows: list[dict[str, Any]] = []
    log(f"{len(todo)} partidas pendientes ({len(done)} ya hechas); gasto previo {spent_before:.4f} USD")

    def run_one(game: ScheduledGame) -> None:
        if stop.is_set():
            return
        if budget.exhausted:
            stop.set()
            stopped.append("presupuesto agotado")
            return
        row, board = play_game(game, ctx, max_plies)
        with write_lock:
            with games_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            if row["resultado"] != "*":
                pgn = to_pgn(row, board, event, elos)
                (out_dir / "pgn" / f"{row['id']}.pgn").write_text(pgn, encoding="utf-8")
                with (out_dir / "partidas.pgn").open("a", encoding="utf-8") as fh:
                    fh.write(pgn)
            new_rows.append(row)
            log(
                f"[{len(new_rows)}/{len(todo)}] {row['blancas']} - {row['negras']}: {row['resultado']} "
                f"({row['terminacion']}, {row['plies']} plies) · gasto total {budget.spent_usd:.4f} USD"
            )
            if row["terminacion"] == "presupuesto":
                stop.set()
                stopped.append("presupuesto agotado")
            elif row["terminacion"] == "error_api":
                consecutive_errors[0] += 1
                log(f"   error: {row['error']}")
                if consecutive_errors[0] >= MAX_CONSECUTIVE_ERRORS:
                    stop.set()
                    stopped.append(f"{MAX_CONSECUTIVE_ERRORS} errores de API seguidos")
            else:
                consecutive_errors[0] = 0

    workers = max(1, int(cfg.get("paralelo", 1)))
    try:
        if workers == 1:
            for g in todo:
                if stop.is_set():
                    break
                run_one(g)
        else:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                list(pool.map(run_one, todo))
    except KeyboardInterrupt:
        stopped.append("interrumpido")

    rows = _read_jsonl(games_path)
    estimates = estimate_from_rows(rows, cfg)
    report = render_report(cfg, rows, estimates, budget.spent_usd, stopped[0] if stopped else None)
    report_path = out_dir / "resultados.md"
    report_path.write_text(report, encoding="utf-8")
    return ExperimentResult(rows, estimates, budget.spent_usd, stopped[0] if stopped else None, report_path)


# ---------------------------------------------------------------------- análisis
def _final_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """La última fila de cada partida (al relanzar, una partida «*» puede haberse jugado de nuevo)."""
    last: dict[str, dict[str, Any]] = {}
    for r in rows:
        last[r["id"]] = r
    return list(last.values())


def estimate_from_rows(rows: list[dict[str, Any]], cfg: dict[str, Any]) -> dict[str, EloEstimate]:
    anchors = {a["nombre"]: float(a["elo"]) for a in cfg["anclas"]}
    games = [(r["jugador"], r["rival"], float(r["puntos_jugador"])) for r in _final_rows(rows) if r["resultado"] != "*"]
    return estimate(
        games, anchors, bootstrap=int(cfg.get("bootstrap", 1000)), seed=int(cfg.get("semilla", 0)), lo=ELO_LO, hi=ELO_HI
    )


def _pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    return statistics.quantiles(values, n=100, method="inclusive")[max(0, min(98, int(q * 100) - 1))]


def player_metrics(rows: list[dict[str, Any]], name: str) -> dict[str, Any]:
    finals = _final_rows(rows)
    mine = [r for r in finals if name in (r["jugador"], r["rival"])]
    played = [r for r in mine if r["resultado"] != "*"]
    w = d = l_ = 0
    for r in played:
        s = r["puntos_jugador"] if r["jugador"] == name else 1 - r["puntos_jugador"]
        w, d, l_ = w + (s == 1), d + (s == 0.5), l_ + (s == 0)
    forfeits = sum(
        1
        for r in played
        if r["terminacion"] == "ilegal"
        and ((r["blancas"] == name and r["resultado"] == "0-1") or (r["negras"] == name and r["resultado"] == "1-0"))
    )
    llm = [r["llm"][name] for r in mine if name in (r.get("llm") or {})]
    # El gasto incluye las partidas anuladas (errores de API, tope de gasto) y las repetidas al relanzar.
    cost = sum((r.get("llm") or {}).get(name, {}).get("coste_usd", 0.0) for r in rows)
    lat = sorted(x for s in llm for x in s["latencias_ms"])
    moves = sum(s["jugadas"] for s in llm)
    return {
        "partidas": len(played),
        "ganadas": w,
        "tablas": d,
        "perdidas": l_,
        "puntos": w + 0.5 * d,
        "derrotas_ilegal": forfeits,
        "tablas_tope": sum(1 for r in played if r["terminacion"] == "tope_plies"),
        "anuladas": sum(1 for r in mine if r["resultado"] == "*"),
        "llm": bool(llm),
        "jugadas_llm": moves,
        "intentos": sum(s["intentos"] for s in llm),
        "ilegales": sum(s["ilegales"] for s in llm),
        "legal_primera": sum(s["legal_a_la_primera"] for s in llm),
        "tokens_entrada": sum(s["tokens_entrada"] for s in llm),
        "tokens_salida": sum(s["tokens_salida"] for s in llm),
        "coste_usd": cost,
        "lat_p50": _pct(lat, 0.5),
        "lat_p95": _pct(lat, 0.95),
    }


def _fmt_elo(est: EloEstimate | None) -> tuple[str, str]:
    if est is None:
        return "—", "—"  # sin partidas válidas
    if est.rating is None:
        pct = "0 %" if est.score <= 0 else "100 %"
        return f"sin estimar ({pct})", "—"
    if est.ci_low is None:
        return f"{est.rating:.0f}", "—"

    def bound(v: float) -> str:  # réplicas sin máximo finito quedan en el borde del dominio: sin cota
        return "−∞" if v <= ELO_LO + 1e-6 else "+∞" if v >= ELO_HI - 1e-6 else f"{v:.0f}"

    return f"{est.rating:.0f}", f"{bound(est.ci_low)} – {bound(est.ci_high)}"


def render_report(
    cfg: dict[str, Any],
    rows: list[dict[str, Any]],
    estimates: dict[str, EloEstimate],
    spent: float,
    stopped: str | None,
) -> str:
    finals = _final_rows(rows)
    played = [r for r in finals if r["resultado"] != "*"]
    budget = cfg.get("presupuesto_usd")
    lines = [
        f"# Experimento «{cfg.get('nombre', 'experimento')}»",
        "",
        f"Fecha: {time.strftime('%Y-%m-%d %H:%M')}. Partidas válidas: **{len(played)}** "
        f"(anuladas: {len(finals) - len(played)}). Gasto en modelos: **{spent:.4f} USD**"
        + (f" de un tope de {float(budget):.2f} USD" if budget is not None else "")
        + ".",
    ]
    if stopped:
        lines += ["", f"> Experimento detenido: **{stopped}**. Los resultados son parciales."]
    oc = cfg.get("aperturas") or {}
    lines += [
        "",
        f"Reglas: posiciones iniciales tras {oc.get('plies_min', 4)}–{oc.get('plies_max', 8)} plies de partidas "
        f"amateur reales (sin repetir, {'cada una con los dos colores' if oc.get('ambos_colores', True) else 'colores alternos'}); "
        f"fin por `board.outcome(claim_draw=True)` o tope de {cfg.get('tope_plies', 300)} plies (tablas, anotadas aparte); "
        f"un LLM que agota {(cfg.get('llm') or {}).get('max_reintentos', 3)} reintentos pierde por ilegalidad; "
        "los errores de API anulan la partida (no cuentan).",
        "",
        "## Jugadores",
        "",
        "| Jugador | Configuración | Elo fijo |",
        "|---|---|--:|",
    ]
    for spec in cfg["anclas"]:
        lines.append(f"| {spec['nombre']} | {describe_spec(spec)} | {spec['elo']} |")
    for spec in cfg["participantes"]:
        lines.append(f"| {spec['nombre']} | {describe_spec(spec)} | — |")

    lines += [
        "",
        "## Elo estimado",
        "",
        "Máxima verosimilitud (modelo logístico, tablas = ½) contra las anclas de Elo fijo; IC 95 % por bootstrap "
        f"({cfg.get('bootstrap', 1000)} réplicas, remuestreo dentro de cada emparejamiento). Con 0 % o 100 % de "
        "puntos no hay máximo finito y no se da cifra.",
        "",
        "| Jugador | Partidas | +/=/− | Puntos | Elo | IC 95 % | Derrotas por ilegal | Tablas por tope | Anuladas |",
        "|---|--:|:-:|--:|--:|:-:|--:|--:|--:|",
    ]
    for spec in cfg["participantes"]:
        name = spec["nombre"]
        m = player_metrics(rows, name)
        elo, ci = _fmt_elo(estimates.get(name))
        pct = f"{m['puntos'] / m['partidas']:.0%}" if m["partidas"] else "—"
        lines.append(
            f"| {name} | {m['partidas']} | {m['ganadas']}/{m['tablas']}/{m['perdidas']} | {m['puntos']:g} ({pct}) | "
            f"{elo} | {ci} | {m['derrotas_ilegal']} | {m['tablas_tope']} | {m['anuladas']} |"
        )

    llm_specs = [s for s in cfg["participantes"] if s["tipo"] in LLM_TYPES]
    if llm_specs:
        lines += [
            "",
            "## Métricas de los LLM",
            "",
            "| Jugador | Jugadas | Ilegales / 100 propuestas | Legal a la 1.ª | Tokens entrada / salida por jugada | "
            "Coste total | Coste por partida | Latencia p50 / p95 por llamada |",
            "|---|--:|--:|--:|--:|--:|--:|--:|",
        ]
        for spec in llm_specs:
            m = player_metrics(rows, spec["nombre"])
            jm = m["jugadas_llm"] or 1
            ill = f"{100 * m['ilegales'] / m['intentos']:.1f}" if m["intentos"] else "—"
            first = f"{m['legal_primera'] / jm:.0%}" if m["jugadas_llm"] else "—"
            lat = "—" if m["lat_p50"] is None else f"{m['lat_p50'] / 1000:.1f} s / {m['lat_p95'] / 1000:.1f} s"
            per_game = f"{m['coste_usd'] / m['partidas']:.4f}" if m["partidas"] else "—"
            lines.append(
                f"| {spec['nombre']} | {m['jugadas_llm']} | {ill} | {first} | "
                f"{m['tokens_entrada'] / jm:.0f} / {m['tokens_salida'] / jm:.0f} | {m['coste_usd']:.4f} $ | "
                f"{per_game} $ | {lat} |"
            )

    lines += [
        "",
        "## Por emparejamiento",
        "",
        "| Jugador | Rival | Partidas | Puntos del jugador | Terminaciones |",
        "|---|---|--:|--:|---|",
    ]
    pairs: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for r in played:
        pairs[(r["jugador"], r["rival"])].append(r)
    for (a, b), rs in sorted(pairs.items()):
        pts = sum(r["puntos_jugador"] for r in rs)
        terms: dict[str, int] = defaultdict(int)
        for r in rs:
            terms[r["terminacion"]] += 1
        tt = ", ".join(f"{k} {v}" for k, v in sorted(terms.items()))
        lines.append(f"| {a} | {b} | {len(rs)} | {pts:g} ({pts / len(rs):.0%}) | {tt} |")

    lines += [
        "",
        "## Notas",
        "",
        "- El Elo de las anclas de Stockfish es la escala `UCI_Elo` (calibrada en CCRL Blitz a 120 s + 1 s); con un "
        "límite fijo por nodos o por tiempo la fuerza real puede diferir. No es comparable con Elo FIDE, Lichess "
        "ni Chess.com.",
        "- Las posiciones iniciales vienen de partidas reales de Chess.com; los rivales aparecen solo como "
        "«rival (Elo N)».",
        "- Puntuaciones entre el 35 % y el 65 % contra un ancla son las que más informan; si un modelo gana o pierde "
        "todo contra un ancla, añade una más fuerte o más débil.",
        "",
    ]
    return "\n".join(lines)
