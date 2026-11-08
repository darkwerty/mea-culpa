"""Pistas: la mejor jugada del motor, los motivos (hechos deducidos con python-chess y Stockfish) y una explicación.

Como en el diagnóstico, el motor decide y el LLM solo explica. Los motivos son frases en castellano generadas en
código; el LLM las convierte en 2 o 3 frases y, si falla (error, sin cuota o sin presupuesto), la explicación se
monta con los propios motivos.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import chess

from .config import Settings
from .engines import PIECE_VALUES
from .live import Line
from .llm.base import LLMProvider
from .prompts import hint_messages
from .schemas import Pista

PIECES = {
    chess.PAWN: "el peón",
    chess.KNIGHT: "el caballo",
    chess.BISHOP: "el alfil",
    chess.ROOK: "la torre",
    chess.QUEEN: "la dama",
    chess.KING: "el rey",
}
PROMOTIONS = {chess.QUEEN: "dama", chess.ROOK: "torre", chess.BISHOP: "alfil", chess.KNIGHT: "caballo"}
CENTRE = {chess.D4, chess.E4, chess.D5, chess.E5}
CLEAR_GAP_CP = 150  # diferencia con la segunda mejor jugada a partir de la cual se dice que es «claramente» mejor
MAX_EXPLANATION = 700
_FORBIDDEN = str.maketrans({"·": ",", "—": ",", "–": "-"})
_SPACES = re.compile(r"\s+")
_COMMAS = re.compile(r"\s*,(?:\s*,)*\s+|\s+,(?:\s*,)*")  # no toca la coma decimal (0,3)


def _value(piece_type: int) -> int:
    return PIECE_VALUES[piece_type] or 10_000


def _piece_at(board: chess.Board, sq: chess.Square) -> str:
    piece = board.piece_at(sq)
    return f"{PIECES[piece.piece_type]} de {chess.square_name(sq)}" if piece else chess.square_name(sq)


def _en_prise(board: chess.Board, sq: chess.Square) -> bool:
    """La pieza de `sq` se puede capturar con ganancia: sin defensa o atacada por una pieza que vale menos."""
    piece = board.piece_at(sq)
    if piece is None or piece.piece_type == chess.KING:
        return False
    attackers = board.attackers(not piece.color, sq)
    if not attackers:
        return False
    if not board.attackers(piece.color, sq):
        return True
    cheapest = min(_value(board.piece_type_at(a) or chess.PAWN) for a in attackers)
    return cheapest < _value(piece.piece_type)


def format_score(line: Line, color: chess.Color) -> str:
    """Valoración legible desde `color`, con coma decimal."""
    if line.mate is not None:
        n = line.mate if color == chess.WHITE else -line.mate
        if n == 0:
            return "jaque mate"
        return f"mate en {abs(n)} a tu favor" if n > 0 else f"mate en {abs(n)} en tu contra"
    cp = (line.cp or 0) * (1 if color == chess.WHITE else -1)
    text = f"{cp / 100:+.1f}".replace(".", ",")
    if abs(cp) < 30:
        return f"{text} (igualada)"
    return f"{text} a tu favor" if cp > 0 else f"{text} en tu contra"


def motives(board: chess.Board, move: chess.Move, lines: list[Line], after: Line | None = None) -> list[str]:
    """Por qué el motor elige `move`, en frases cortas y verificables. `lines`: líneas del motor (la 1.ª es la mejor)."""
    side = board.turn
    piece = board.piece_at(move.from_square)
    assert piece is not None
    out: list[str] = []
    nxt = board.copy(stack=False)
    nxt.push(move)
    best = lines[0] if lines else None

    if nxt.is_checkmate():
        return ["Da jaque mate: la partida se acaba aquí."]
    if best is not None and best.mate is not None:
        n = best.mate if side == chess.WHITE else -best.mate
        if n > 0:
            out.append(f"Fuerza el mate en {n} si sigues la línea del motor.")

    if board.is_check():
        if piece.piece_type == chess.KING:
            out.append("Saca al rey del jaque a una casilla segura.")
        elif board.is_capture(move) and move.to_square in board.checkers():
            out.append("Responde al jaque capturando la pieza que lo da.")
        else:
            out.append("Tapa el jaque con una pieza.")

    if board.is_castling(move):
        out.append("Enroca: el rey sale del centro y queda protegido, y la torre entra en juego.")

    if board.is_capture(move):
        if board.is_en_passant(move):
            victim_type, victim = chess.PAWN, "el peón al paso"
        else:
            victim_type = board.piece_type_at(move.to_square) or chess.PAWN
            victim = _piece_at(board, move.to_square)
        if not nxt.is_attacked_by(not side, move.to_square):
            out.append(f"Captura {victim} y el rival no puede recapturar: ganas material.")
        elif _value(victim_type) > _value(piece.piece_type):
            out.append(f"Captura {victim} con {PIECES[piece.piece_type]}, que vale menos: el cambio te favorece.")
        else:
            out.append(f"Captura {victim}.")

    if move.promotion:
        out.append(f"Corona el peón en {PROMOTIONS.get(move.promotion, 'dama')}.")

    if nxt.is_check():
        out.append("Da jaque y obliga al rival a responder.")

    # Amenazas nuevas de la pieza que mueve: piezas rivales sin defensa o que valen más que ella.
    targets = [
        sq
        for sq in nxt.attacks(move.to_square)
        if nxt.color_at(sq) == (not side)
        and nxt.piece_type_at(sq) != chess.KING
        and (not nxt.attackers(not side, sq) or _value(nxt.piece_type_at(sq) or chess.PAWN) > _value(piece.piece_type))
    ]
    targets.sort(key=lambda sq: -_value(nxt.piece_type_at(sq) or chess.PAWN))
    if len(targets) >= 2 or (targets and nxt.is_check()):
        names = " y ".join(_piece_at(nxt, sq) for sq in targets[:2])
        extra = "da jaque y además ataca" if nxt.is_check() and len(targets) == 1 else "ataca a la vez"
        out.append(f"Ataque doble: {extra} {names}.")
    elif targets:
        out.append(f"Amenaza {_piece_at(nxt, targets[0])}.")

    # Defensa: piezas propias que el rival podía capturar con ganancia y que dejan de estar en peligro.
    for sq in chess.SquareSet(board.occupied_co[side]):
        if sq == move.to_square or board.piece_type_at(sq) == chess.PAWN or not _en_prise(board, sq):
            continue
        if sq == move.from_square:
            if not _en_prise(nxt, move.to_square):
                out.append(f"Pone a salvo {_piece_at(board, sq)}, que el rival podía capturar.")
        elif not _en_prise(nxt, sq):
            out.append(f"Quita la amenaza sobre {_piece_at(board, sq)}.")

    if board.fullmove_number <= 12 and not board.is_castling(move):
        home = 0 if side == chess.WHITE else 7
        if piece.piece_type in (chess.KNIGHT, chess.BISHOP) and chess.square_rank(move.from_square) == home:
            out.append(f"Desarrolla {PIECES[piece.piece_type]} y lo pone en juego.")
        elif piece.piece_type == chess.PAWN and move.to_square in CENTRE:
            out.append("Ocupa el centro con un peón.")
        elif piece.piece_type == chess.KING and board.has_castling_rights(side):
            out.append("Mover el rey te quita el enroque, pero aquí es lo mejor según el motor.")

    if len(lines) >= 2 and lines[1].pv:
        gap = lines[0].user_cp(side) - lines[1].user_cp(side)
        if gap >= CLEAR_GAP_CP:
            second = board.san(lines[1].pv[0]) if lines[1].pv[0] in board.legal_moves else None
            if second:
                out.append(
                    f"Es claramente la mejor: la siguiente opción, {second}, deja la valoración en {format_score(lines[1], side)}."
                )

    if after is not None or best is not None:
        ref = after if after is not None else best
        assert ref is not None
        if not any(m.startswith("Fuerza el mate") for m in out):
            out.append(f"Con ella la valoración queda en {format_score(ref, side)}.")
    if all(m.startswith("Con ella la valoración") for m in out):
        out.insert(0, "Mejora la posición sin perder material: es la que el motor prefiere entre todas.")
    return out


def clean_text(text: str, max_len: int = MAX_EXPLANATION) -> str:
    """Sin puntos medios ni guiones largos (estilo de la web), espacios normalizados y recortado en una frase."""
    text = _SPACES.sub(" ", str(text).translate(_FORBIDDEN)).strip()
    text = _COMMAS.sub(", ", text).strip(" ,")
    if len(text) > max_len:
        cut = text[:max_len]
        text = cut[: cut.rfind(".") + 1] if "." in cut else cut.rstrip() + "."
    return text


def fallback_explanation(best_san: str, reasons: list[str]) -> str:
    """Explicación sin LLM: la jugada y los dos o tres motivos más importantes."""
    picked = [r for r in reasons if not r.startswith("Con ella la valoración")][:2]
    tail = [r for r in reasons if r.startswith("Con ella la valoración")]
    body = " ".join([*picked, *tail[:1]])
    return clean_text(f"El motor recomienda {best_san}. {body}".strip())


@dataclass
class HintText:
    explicacion: str
    fuente: str  # "llm" | "motor"
    modelo: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    coste_usd: float = 0.0
    error: str | None = None


def explain(
    provider: LLMProvider,
    board: chess.Board,
    best_san: str,
    line_san: list[str],
    reasons: list[str],
    settings: Settings,
) -> HintText:
    """2 o 3 frases del LLM a partir de los motivos. Si algo falla, explicación generada con los motivos."""
    side = "blancas" if board.turn == chess.WHITE else "negras"
    messages = hint_messages(board.fen(), side, best_san, line_san, reasons)
    try:
        res = provider.structured(messages, Pista, None)
    except Exception as exc:  # noqa: BLE001 - red, 4xx/5xx del proveedor: la pista sale igual
        return HintText(fallback_explanation(best_san, reasons), "motor", error=f"{type(exc).__name__}: {exc}"[:200])
    cost = (res.input_tokens * settings.price_input_per_m + res.output_tokens * settings.price_output_per_m) / 1e6
    base: dict[str, Any] = {
        "modelo": res.model,
        "input_tokens": res.input_tokens,
        "output_tokens": res.output_tokens,
        "coste_usd": round(cost, 6),
    }
    text = clean_text(res.parsed.explicacion) if res.parsed is not None else ""
    if len(text) < 20:
        return HintText(fallback_explanation(best_san, reasons), "motor", error=res.error or "respuesta vacía", **base)
    return HintText(text, "llm", **base)
