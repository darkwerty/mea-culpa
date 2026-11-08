"""Análisis "mea culpa": el motor encuentra los errores, el LLM solo los explica.

1. Se evalúa cada posición de la partida (Stockfish si está instalado; si no, nuestro alfa-beta).
2. Para cada jugada del usuario: caída = eval(antes) - eval(después), desde su punto de vista.
3. Las jugadas con caída grande se etiquetan con patrones deterministas (pieza colgada, mate
   permitido, rey en el centro...). Esos son los HECHOS.
4. El LLM recibe solo esos hechos y devuelve una frase por error + patrones recurrentes.
   Su salida se valida contra los hechos: lo que no esté en ellos se descarta y se cuenta.
"""

from __future__ import annotations

import io
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

import chess
import chess.engine
import chess.pgn

from .config import Settings, get_settings
from .engines import PIECE_VALUES, AlphaBetaSearcher, find_stockfish, is_mate_score, mate_in_moves
from .engines.alphabeta import MATE
from .llm.base import LLMProvider
from .prompts import diagnosis_messages
from .schemas import PATTERNS, Diagnostico

CLIP_CP = 1000
SEVERITY = [(300, "grave"), (150, "error"), (70, "imprecision")]
PIECE_NAMES = {
    chess.PAWN: "el peón",
    chess.KNIGHT: "el caballo",
    chess.BISHOP: "el alfil",
    chess.ROOK: "la torre",
    chess.QUEEN: "la dama",
    chess.KING: "el rey",
}
TAG_PRIORITY = [
    "mate_permitido",
    "mate_no_visto",
    "pieza_colgada",
    "cambio_desfavorable",
    "oportunidad_perdida",
    "dama_prematura",
    "enroque_tardio",
    "rey_en_el_centro",
    "error_posicional",
]


# ------------------------------------------------------------------ motores
@dataclass
class PositionEval:
    score: int  # centipeones desde el bando que mueve; mates como MATE - ply
    best: chess.Move | None


class Analyzer(Protocol):
    name: str

    def evaluate(self, board: chess.Board, extra_depth: int = 0) -> PositionEval: ...


class AlphaBetaAnalyzer:
    def __init__(self, depth: int = 3) -> None:
        self.depth = depth
        self.name = f"alfa-beta propio (profundidad {depth})"
        # Una sola tabla de transposiciones para toda la partida: posiciones consecutivas
        # comparten muchos subárboles.
        self._searcher = AlphaBetaSearcher(depth=depth)

    def evaluate(self, board: chess.Board, extra_depth: int = 0) -> PositionEval:
        res = self._searcher.search(board, depth=self.depth + extra_depth)
        return PositionEval(res.score, res.move)

    def close(self) -> None:
        pass


class StockfishAnalyzer:
    def __init__(self, path: str, depth: int = 14) -> None:
        self.depth = depth
        self.name = f"Stockfish (profundidad {depth})"
        self._engine = chess.engine.SimpleEngine.popen_uci(path)

    def evaluate(self, board: chess.Board, extra_depth: int = 0) -> PositionEval:
        info = self._engine.analyse(board, chess.engine.Limit(depth=self.depth + extra_depth))
        pov = info["score"].pov(board.turn)
        mate = pov.mate()
        if mate is not None:
            score = (MATE - (2 * mate - 1)) if mate > 0 else -(MATE - 2 * abs(mate))
        else:
            score = pov.score() or 0
        pv = info.get("pv") or []
        return PositionEval(score, pv[0] if pv else None)

    def close(self) -> None:
        self._engine.quit()


LONG_GAME_PLIES = 120


def make_analyzer(settings: Settings | None = None, plies: int = 0) -> AlphaBetaAnalyzer | StockfishAnalyzer:
    """Stockfish si está instalado; si no, nuestro alfa-beta.

    En partidas muy largas (> LONG_GAME_PLIES) el alfa-beta baja un ply para que el análisis
    no pase de unas decenas de segundos en Python puro.
    """
    settings = settings or get_settings()
    path = find_stockfish()
    if path:
        return StockfishAnalyzer(path, settings.stockfish_depth)
    depth = settings.analysis_depth
    if plies > LONG_GAME_PLIES:
        depth = max(2, depth - 1)
    return AlphaBetaAnalyzer(depth)


# --------------------------------------------------------------- utilidades
def clip(score: int) -> int:
    return max(-CLIP_CP, min(CLIP_CP, score))


def format_eval(score: int) -> str:
    """Texto legible desde el punto de vista del jugador."""
    if is_mate_score(score):
        n = mate_in_moves(score)
        return f"mate a favor en {n}" if n > 0 else f"mate en contra en {abs(n)}"
    return f"{score / 100:+.1f}"


def severity_for(drop: int) -> str | None:
    for threshold, name in SEVERITY:
        if drop >= threshold:
            return name
    return None


def _hanging_piece(board_after: chess.Board, user: chess.Color, reply: chess.Move | None) -> tuple[str, str] | None:
    """Pieza del usuario que el rival puede capturar con ventaja (y que su mejor respuesta captura)."""
    if reply is None or not board_after.is_capture(reply):
        return None
    sq = reply.to_square
    piece = board_after.piece_at(sq)
    if piece is None or piece.color != user or piece.piece_type == chess.KING:
        return None
    attackers = board_after.attackers(not user, sq)
    defenders = board_after.attackers(user, sq)
    min_attacker = min((PIECE_VALUES[board_after.piece_type_at(a) or chess.PAWN] or 10_000) for a in attackers)
    value = PIECE_VALUES[piece.piece_type]
    if not defenders or min_attacker < value:
        return PIECE_NAMES[piece.piece_type], chess.square_name(sq)
    return None


def _castled_move_number(game_moves: list[tuple[chess.Board, chess.Move]], user: chess.Color) -> int | None:
    for b, m in game_moves:
        if b.turn == user and b.is_castling(m):
            return b.fullmove_number
    return None


def _king_in_centre(board: chess.Board, user: chess.Color) -> bool:
    k = board.king(user)
    if k is None:
        return False
    home_rank = 0 if user == chess.WHITE else 7
    return chess.square_file(k) in (3, 4, 5) and abs(chess.square_rank(k) - home_rank) <= 1


# ----------------------------------------------------------------- análisis
@dataclass
class MoveEval:
    ply: int
    jugada_numero: int
    color: str
    jugada: str
    uci: str
    eval_antes_cp: int
    eval_despues_cp: int
    caida_cp: int
    mejor_jugada: str | None
    gravedad: str | None
    etiquetas: list[str] = field(default_factory=list)
    detalle: dict[str, str] = field(default_factory=dict)

    def to_fact(self) -> dict[str, Any]:
        return {
            "ply": self.ply,
            "jugada_numero": self.jugada_numero,
            "jugada": self.jugada,
            "eval_antes": format_eval(self.eval_antes_cp),
            "eval_despues": format_eval(self.eval_despues_cp),
            "caida_cp": self.caida_cp,
            "mejor_jugada": self.mejor_jugada,
            "gravedad": self.gravedad,
            "etiquetas": self.etiquetas,
            "detalle": self.detalle,
        }


@dataclass
class GameAnalysis:
    motor: str
    user_color: str
    jugadas_usuario: list[MoveEval]
    curva_blancas: list[int]  # evaluación (cp, recortada) desde las blancas, posición a posición
    hechos_partida: dict[str, Any]
    patrones_partida: list[str]

    def errors(self, max_items: int = 5) -> list[MoveEval]:
        serious = [m for m in self.jugadas_usuario if m.gravedad in ("grave", "error")]
        if not serious:
            serious = [m for m in self.jugadas_usuario if m.gravedad == "imprecision"]
            max_items = min(max_items, 3)
        top = sorted(serious, key=lambda m: -m.caida_cp)[:max_items]
        return sorted(top, key=lambda m: m.ply)

    def biggest_drop(self) -> MoveEval | None:
        return max(self.jugadas_usuario, key=lambda m: m.caida_cp, default=None)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["errores"] = [m.to_fact() for m in self.errors()]
        return d


class AnalysisTimeout(Exception):
    """El análisis superó su tiempo máximo."""


def analyze_moves(
    moves: list[chess.Move],
    user_color: chess.Color,
    analyzer: Analyzer,
    start_fen: str = chess.STARTING_FEN,
    deadline: float | None = None,
) -> GameAnalysis:
    """`deadline` (time.monotonic) corta el análisis entre posiciones si se pasa de tiempo."""
    board = chess.Board(start_fen)
    positions: list[chess.Board] = [board.copy()]
    pairs: list[tuple[chess.Board, chess.Move]] = []
    for m in moves:
        pairs.append((board.copy(), m))
        board.push(m)
        positions.append(board.copy())

    evals: list[PositionEval] = []
    for pos in positions:
        if deadline is not None and time.monotonic() > deadline:
            raise AnalysisTimeout()
        if pos.is_checkmate():
            evals.append(PositionEval(-MATE, None))
        elif pos.is_game_over():
            evals.append(PositionEval(0, None))
        else:
            # Horizonte coherente: la posición ANTES de la jugada del usuario se busca un ply más
            # que la de DESPUÉS. Así eval(antes) = max sobre jugadas de -eval(después) y la caída
            # de la mejor jugada es exactamente 0 (con alfa-beta a profundidad fija).
            evals.append(analyzer.evaluate(pos, extra_depth=1 if pos.turn == user_color else 0))

    curva = [clip(e.score if p.turn == chess.WHITE else -e.score) for p, e in zip(positions, evals, strict=True)]
    castled_at = _castled_move_number(pairs, user_color)

    user_moves: list[MoveEval] = []
    for i, (before, move) in enumerate(pairs):
        if before.turn != user_color:
            continue
        after = positions[i + 1]
        score_before = evals[i].score
        score_after = -evals[i + 1].score
        best = evals[i].best
        drop = 0 if best == move else clip(score_before) - clip(score_after)
        tags: list[str] = []
        detail: dict[str, str] = {}
        if is_mate_score(score_after) and score_after < 0 and not (is_mate_score(score_before) and score_before < 0):
            tags.append("mate_permitido")
        if is_mate_score(score_before) and score_before > 0 and not (is_mate_score(score_after) and score_after > 0):
            tags.append("mate_no_visto")
        hanging = _hanging_piece(after, user_color, evals[i + 1].best)
        if hanging and drop >= 150:
            tags.append("pieza_colgada")
            detail["pieza_en_peligro"] = f"{hanging[0]} de {hanging[1]}"
        if before.is_capture(move) and drop >= 150 and "pieza_colgada" not in tags:
            tags.append("cambio_desfavorable")
        if best is not None and best != move and before.is_capture(best) and drop >= 150:
            victim = before.piece_type_at(best.to_square)
            if victim and PIECE_VALUES[victim] >= 300 and not tags:
                tags.append("oportunidad_perdida")
        piece = before.piece_type_at(move.from_square)
        if piece == chess.QUEEN and before.fullmove_number <= 6 and drop >= 70:
            tags.append("dama_prematura")
        if drop >= 70 and before.fullmove_number >= 10 and _king_in_centre(before, user_color):
            if castled_at is None and before.has_castling_rights(user_color):
                tags.append("enroque_tardio")
            tags.append("rey_en_el_centro")
        if not tags:
            tags.append("error_posicional")
        tags.sort(key=TAG_PRIORITY.index)
        user_moves.append(
            MoveEval(
                ply=i + 1,
                jugada_numero=before.fullmove_number,
                color="blancas" if user_color == chess.WHITE else "negras",
                jugada=before.san(move),
                uci=move.uci(),
                eval_antes_cp=score_before,
                eval_despues_cp=score_after,
                caida_cp=max(0, drop),
                mejor_jugada=before.san(best) if best is not None else None,
                gravedad=severity_for(drop),
                etiquetas=tags,
                detalle=detail,
            )
        )

    last_move_number = positions[-1].fullmove_number
    hechos: dict[str, Any] = {
        "jugadas_totales": len(moves),
        "enroque": f"jugada {castled_at}" if castled_at else "no enrocó",
    }
    game_patterns: set[str] = set()
    for m in user_moves:
        if m.gravedad in ("grave", "error"):
            game_patterns.update(t for t in m.etiquetas if t != "error_posicional")
    if (castled_at is None and last_move_number > 12) or (castled_at is not None and castled_at > 12):
        game_patterns.add("enroque_tardio")
    return GameAnalysis(
        motor=analyzer.name,
        user_color="blancas" if user_color == chess.WHITE else "negras",
        jugadas_usuario=user_moves,
        curva_blancas=curva,
        hechos_partida=hechos,
        patrones_partida=sorted(game_patterns, key=TAG_PRIORITY.index),
    )


def parse_pgn(pgn_text: str) -> chess.pgn.Game:
    if len(pgn_text) > 20_000:
        raise ValueError("PGN demasiado largo")
    game = chess.pgn.read_game(io.StringIO(pgn_text))
    if game is None:
        raise ValueError("No se pudo leer el PGN")
    if game.errors:
        raise ValueError(f"PGN con errores: {game.errors[0]}")
    return game


VALID_RESULTS = {"1-0", "0-1", "1/2-1/2", "*"}


def pgn_without_comments(game: chess.pgn.Game) -> str:
    """Reconstruye el PGN solo con la línea principal: sin comentarios, sin variantes y sin cabeceras de
    texto libre (Event, White, Annotator...), que son una vía de inyección de prompt. Se conservan solo
    FEN/SetUp (validados al crear el tablero) y Result (de una lista cerrada)."""
    clean = chess.pgn.Game()
    clean.headers.clear()
    board = game.board()
    if board.fen() != chess.STARTING_FEN:
        clean.setup(board)
    result = game.headers.get("Result", "*")
    clean.headers["Result"] = result if result in VALID_RESULTS else "*"
    node: chess.pgn.GameNode = clean
    for move in game.mainline_moves():
        node = node.add_variation(move)
    exporter = chess.pgn.StringExporter(headers=True, variations=False, comments=False)
    return clean.accept(exporter)


# -------------------------------------------------------------- diagnóstico
@dataclass
class DiagnosisResult:
    diagnostico: dict[str, Any] | None
    grounding: dict[str, Any]
    modelo: str
    input_tokens: int
    output_tokens: int
    latency_ms: float
    coste_usd: float
    error: str | None = None


def history_counts(previous_patterns: list[list[str]], current: list[str]) -> dict[str, Any]:
    """Cuántas partidas (incluida esta) contienen cada patrón."""
    games = [*previous_patterns, current]
    counts: dict[str, int] = {}
    for pats in games:
        for p in set(pats):
            counts[p] = counts.get(p, 0) + 1
    return {"partidas": len(games), "recuentos": counts, "nota": "los recuentos incluyen esta partida"}


def diagnose(
    analysis: GameAnalysis,
    provider: LLMProvider,
    pgn: str,
    result_text: str,
    history: dict[str, Any],
    temperature: float | None = None,  # None = no se envía (valor por defecto del proveedor)
    settings: Settings | None = None,
) -> DiagnosisResult:
    settings = settings or get_settings()
    errors = analysis.errors()
    facts = [e.to_fact() for e in errors]
    messages = diagnosis_messages(pgn, result_text, analysis.user_color, facts, history)
    res = provider.structured(messages, Diagnostico, temperature)
    cost = (res.input_tokens * settings.price_input_per_m + res.output_tokens * settings.price_output_per_m) / 1e6
    base = {
        "modelo": res.model,
        "input_tokens": res.input_tokens,
        "output_tokens": res.output_tokens,
        "latency_ms": round(res.latency_ms, 1),
        "coste_usd": round(cost, 6),
    }
    if res.parsed is None:
        return DiagnosisResult(None, {}, error=res.error or "respuesta no válida", **base)
    return DiagnosisResult(*ground(res.parsed, errors, history), **base)


def ground(diag: Diagnostico, errors: list[MoveEval], history: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Valida la salida del LLM contra los hechos del motor. Lo inventado se descarta."""
    by_ply = {e.ply: e for e in errors}
    kept, invented, fixed_patterns, seen = [], 0, 0, set()
    for item in diag.errores:
        fact = by_ply.get(item.ply)
        if fact is None or item.ply in seen:
            invented += 1
            continue
        seen.add(item.ply)
        allowed = set(fact.etiquetas) | {"error_posicional", "otro"}
        patron = item.patron
        if patron not in allowed:
            fixed_patterns += 1
            patron = fact.etiquetas[0]
        kept.append(
            {
                "ply": item.ply,
                "jugada_numero": fact.jugada_numero,
                "jugada": fact.jugada,
                "explicacion": item.explicacion,
                "patron": patron,
                "patron_texto": PATTERNS.get(patron, patron),
                "eval_antes": format_eval(fact.eval_antes_cp),
                "eval_despues": format_eval(fact.eval_despues_cp),
                "mejor_jugada": fact.mejor_jugada,
                "gravedad": fact.gravedad,
            }
        )
    principal = diag.error_principal_ply
    principal_fixed = False
    if errors and principal not in by_ply:
        principal = max(errors, key=lambda e: e.caida_cp).ply
        principal_fixed = True
    counts = history.get("recuentos", {})
    recurrent, recurrent_dropped = [], 0
    for r in diag.patrones_recurrentes:
        if counts.get(r.patron, 0) >= 2:
            recurrent.append(
                {
                    "patron": r.patron,
                    "patron_texto": PATTERNS[r.patron],
                    "descripcion": r.descripcion,
                    "partidas": counts[r.patron],
                }
            )
        else:
            recurrent_dropped += 1
    diagnostico = {
        "titular": diag.titular,
        "error_principal_ply": principal,
        "errores": kept,
        "patrones_recurrentes": recurrent,
        "consejo": diag.consejo,
    }
    grounding = {
        "errores_inventados_descartados": invented,
        "patrones_corregidos": fixed_patterns,
        "principal_corregido": principal_fixed,
        "recurrentes_sin_respaldo_descartados": recurrent_dropped,
    }
    return diagnostico, grounding
