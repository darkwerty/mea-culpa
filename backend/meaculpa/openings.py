"""Posiciones iniciales «no famosas» sacadas de partidas reales (p. ej. las de Chess.com de Daniel).

Para no medir memoria de partidas célebres, cada partida del experimento arranca desde la posición que
había tras N plies (4–8 por defecto) de una partida amateur real. Reglas:
- Sin repetir: dos posiciones con la misma EPD (piezas, turno, enroques, al paso) cuentan como una.
- Se descartan posiciones ya terminadas y, si se da un evaluador, las demasiado desequilibradas.
- Los nombres de los rivales NO salen de aquí: el origen se describe como «rival (Elo N)».

`assign_openings` reparte las posiciones entre las partidas de un emparejamiento alternando colores:
con `both_colors=True` cada posición se juega dos veces, una con cada color.
"""

from __future__ import annotations

import io
import random
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

import chess
import chess.pgn


@dataclass(frozen=True)
class StartPosition:
    id: str
    moves_uci: tuple[str, ...]
    fen: str
    plies: int
    eco: str | None = None
    origen: str = ""
    san: tuple[str, ...] = field(default=(), compare=False)

    def board(self) -> chess.Board:
        """Tablero desde la posición inicial estándar con las jugadas de apertura ya hechas (historial completo)."""
        b = chess.Board()
        for uci in self.moves_uci:
            b.push_uci(uci)
        return b

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "plies": self.plies,
            "fen": self.fen,
            "eco": self.eco,
            "origen": self.origen,
            "jugadas": " ".join(self.san),
        }


def read_games(source: str | Path) -> list[chess.pgn.Game]:
    """Lee todas las partidas de un PGN: `Path` = fichero, `str` = el texto PGN."""
    text = source.read_text(encoding="utf-8") if isinstance(source, Path) else source
    stream = io.StringIO(text)
    games = []
    while True:
        game = chess.pgn.read_game(stream)
        if game is None:
            break
        if game.errors:
            continue
        if game.headers.get("Variant", "Standard") not in ("Standard", "Chess", ""):
            continue
        if game.headers.get("SetUp") == "1" or "FEN" in game.headers:
            continue  # solo partidas desde la posición inicial
        games.append(game)
    return games


def _origin(game: chess.pgn.Game, user: str | None) -> str:
    """Descripción del origen sin nombres de terceros: «2026.10.30 · B00 · rival (Elo 1099)»."""
    h = game.headers
    parts = [h.get("Date", "?")]
    if h.get("ECO"):
        parts.append(h["ECO"])
    white, black = h.get("White", ""), h.get("Black", "")
    if user and user.lower() == white.lower():
        parts.append(f"{user} con blancas vs rival (Elo {h.get('BlackElo', '?')})")
    elif user and user.lower() == black.lower():
        parts.append(f"{user} con negras vs rival (Elo {h.get('WhiteElo', '?')})")
    else:
        parts.append(f"rival (Elo {h.get('WhiteElo', '?')}) vs rival (Elo {h.get('BlackElo', '?')})")
    return " · ".join(parts)


def extract_start_positions(
    games: Iterable[chess.pgn.Game],
    *,
    min_plies: int = 4,
    max_plies: int = 8,
    limit: int | None = None,
    seed: int = 0,
    user: str | None = None,
    evaluator: Callable[[chess.Board], int | None] | None = None,
    max_abs_cp: int | None = None,
) -> list[StartPosition]:
    """Una posición por partida como mucho, sin repetir EPD.

    `evaluator(board)` devuelve la evaluación en centipeones (desde blancas) o None; si se da junto a
    `max_abs_cp`, se descartan las posiciones con |eval| mayor (aperturas ya decididas).
    """
    if not 0 < min_plies <= max_plies:
        raise ValueError("Se necesita 0 < plies_min <= plies_max")
    rng = random.Random(seed)
    games = list(games)
    order = list(range(len(games)))
    rng.shuffle(order)
    seen: set[str] = set()
    out: list[StartPosition] = []
    for gi in order:
        game = games[gi]
        mainline = list(game.mainline_moves())
        candidates = list(range(min_plies, max_plies + 1))
        rng.shuffle(candidates)
        for k in candidates:
            if len(mainline) <= k:  # tiene que quedar partida después
                continue
            board = chess.Board()
            san: list[str] = []
            for move in mainline[:k]:
                san.append(board.san(move))
                board.push(move)
            if board.is_game_over(claim_draw=True):
                continue
            key = board.epd()
            if key in seen:
                continue
            if evaluator is not None and max_abs_cp is not None:
                cp = evaluator(board)
                if cp is None or abs(cp) > max_abs_cp:
                    continue
            seen.add(key)
            out.append(
                StartPosition(
                    id=f"g{gi + 1:03d}p{k}",
                    moves_uci=tuple(m.uci() for m in mainline[:k]),
                    fen=board.fen(),
                    plies=k,
                    eco=game.headers.get("ECO"),
                    origen=_origin(game, user),
                    san=tuple(san),
                )
            )
            break
        if limit is not None and len(out) >= limit:
            break
    return out


def assign_openings(
    positions: list[StartPosition], n_games: int, both_colors: bool = True
) -> list[tuple[StartPosition, bool]]:
    """(posición, ¿el primer jugador lleva blancas?) para cada partida de un emparejamiento.

    Alterna colores en cada partida; sin repetir posición (salvo la pareja de colores si `both_colors`).
    """
    per_position = 2 if both_colors else 1
    needed = -(-n_games // per_position)
    if needed > len(positions):
        raise ValueError(
            f"Hacen falta {needed} posiciones distintas para {n_games} partidas y solo hay {len(positions)}"
        )
    plan = []
    for i in range(n_games):
        pos = positions[i // per_position] if both_colors else positions[i]
        plan.append((pos, i % 2 == 0))
    return plan


def stockfish_evaluator(path: str, depth: int = 12) -> tuple[Callable[[chess.Board], int | None], Callable[[], None]]:
    """Evaluador con Stockfish a fuerza completa (centipeones desde blancas) y su función de cierre."""
    import chess.engine

    engine = chess.engine.SimpleEngine.popen_uci(path)
    engine.configure({"Threads": 1, "Hash": 32})

    def evaluate(board: chess.Board) -> int | None:
        info = engine.analyse(board, chess.engine.Limit(depth=depth))
        score = info.get("score")
        return None if score is None else score.white().score(mate_score=100_000)

    return evaluate, engine.quit
