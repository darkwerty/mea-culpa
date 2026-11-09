"""Partidas de Chess.com con su API pública (https://api.chess.com/pub/), sin contraseña ni OAuth.

- Solo se leen tres cosas: el perfil (`/player/{usuario}`, para saber si existe o está cerrada), la lista de archivos
  mensuales (`/player/{usuario}/games/archives`) y los archivos de los últimos meses (`/games/{AAAA}/{MM}`).
- Las peticiones van de una en una por réplica (Chess.com no limita el acceso en serie, pero responde 429 a las
  peticiones en paralelo), con un User-Agent que dice qué aplicación es y cómo contactar, y tiempos de espera
  cortos con un tope para toda la importación. Un 429 no se reintenta: se le dice a la persona que pruebe en un rato.
- Todo lo que llega es dato no fiable: el nombre de usuario se valida antes de construir la URL, las URL de los
  archivos se comprueban contra ese usuario antes de pedirlas y cada PGN se vuelve a leer con python-chess.
- Solo se importan partidas de ajedrez estándar (reglas «chess», posición inicial normal) en las que juega ese
  usuario; las de ajedrez 960 y otras variantes, las que empiezan en otra posición y las de otros jugadores se
  ignoran. De cada partida se guarda lo mínimo: jugadas, resultado, fecha, ritmo, Elo y nombre del rival.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import chess
import chess.pgn

API_BASE = "https://api.chess.com/pub"
USER_AGENT = "MeaCulpa/1.0 (+https://danielalonso.dev/meaculpa; contacto: hola@danielalonso.dev)"
MAX_RESPONSE_BYTES = 16 * 1024 * 1024  # un archivo mensual enorme (miles de partidas de bala) se corta aquí
MAX_PGN_CHARS = 60_000
REQUEST_TIMEOUT_S = 12.0
TOTAL_TIMEOUT_S = 30.0  # toda la importación (perfil, archivos y meses)

_USERNAME = re.compile(r"^[A-Za-z0-9_-]{3,25}$")

# Resultados de Chess.com (white.result / black.result).
DRAW_CODES = {"agreed", "repetition", "stalemate", "insufficient", "50move", "timevsinsufficient"}
# Cómo terminó, visto desde quien pierde (o desde cualquiera de los dos si son tablas).
END_REASONS = {
    "checkmated": "jaque mate",
    "resigned": "abandono",
    "timeout": "tiempo",
    "abandoned": "abandono",
    "agreed": "acuerdo",
    "repetition": "triple repetición",
    "stalemate": "rey ahogado",
    "insufficient": "material insuficiente",
    "50move": "regla de 50 jugadas",
    "timevsinsufficient": "tiempo sin material para ganar",
}
TIME_CLASSES = {"bullet": "bala", "blitz": "blitz", "rapid": "rápida", "daily": "por días", "classical": "clásica"}


class ChessComError(Exception):
    """Error al hablar con Chess.com, con un mensaje para la persona. `status`: el código que devuelve la API."""

    status = 503


class ChessComNotFound(ChessComError):
    status = 404


class ChessComClosed(ChessComError):
    status = 404


class ChessComRateLimited(ChessComError):
    status = 503


class _TooLarge(Exception):
    pass


Fetch = Callable[[str, float], tuple[int, Any]]  # (url, tiempo de espera) -> (código HTTP, JSON o None)


def http_fetch(url: str, timeout: float) -> tuple[int, Any]:
    """GET con requests, User-Agent de la aplicación y tope de tamaño. (código, JSON o None)."""
    import requests

    with requests.get(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        timeout=(min(5.0, timeout), timeout),
        stream=True,
    ) as res:
        if res.status_code != 200:
            return res.status_code, None
        chunks, size = [], 0
        for chunk in res.iter_content(64 * 1024):
            size += len(chunk)
            if size > MAX_RESPONSE_BYTES:
                raise _TooLarge(url)
            chunks.append(chunk)
        return 200, json.loads(b"".join(chunks).decode("utf-8"))


def valid_username(name: str | None) -> str | None:
    """El nombre tal cual si tiene el formato de Chess.com (3 a 25 letras, cifras, _ o -); si no, None."""
    name = (name or "").strip()
    return name if _USERNAME.match(name) else None


def recent_months(now: float, months: int) -> list[tuple[int, int]]:
    """(año, mes) del mes de `now` (UTC) y los `months - 1` anteriores, del más reciente al más antiguo."""
    t = time.gmtime(now)
    year, month = t.tm_year, t.tm_mon
    out = []
    for _ in range(months):
        out.append((year, month))
        year, month = (year, month - 1) if month > 1 else (year - 1, 12)
    return out


class ChessComClient:
    """Cliente mínimo de la API pública. Una petición a la vez por réplica."""

    _lock = threading.Lock()

    def __init__(self, fetch: Fetch | None = None, clock: Callable[[], float] = time.monotonic) -> None:
        self.fetch = fetch or http_fetch
        self.clock = clock

    def _get(self, url: str, deadline: float) -> Any:
        left = deadline - self.clock()
        if left <= 1:
            raise ChessComError("Chess.com está tardando demasiado; vuelve a intentarlo en un rato.")
        if not self._lock.acquire(timeout=min(left, 15)):
            raise ChessComError("Hay otra importación en marcha; vuelve a intentarlo en un momento.")
        try:
            try:
                status, body = self.fetch(url, min(REQUEST_TIMEOUT_S, deadline - self.clock()))
            except (ChessComError, _TooLarge):
                raise
            except Exception as exc:  # noqa: BLE001 - red, tiempo de espera o JSON roto
                raise ChessComError("Chess.com no ha respondido; vuelve a intentarlo en un rato.") from exc
        finally:
            self._lock.release()
        if status == 429:
            raise ChessComRateLimited("Chess.com nos pide que esperemos un poco; vuelve a intentarlo en unos minutos.")
        if status in (404, 410):
            raise ChessComNotFound("not found")
        if status != 200 or not isinstance(body, dict):
            raise ChessComError("Chess.com no ha respondido bien; vuelve a intentarlo en un rato.")
        return body

    def recent_games(
        self, username: str, now: float, months: int, limit: int
    ) -> tuple[list[dict[str, Any]], list[tuple[int, int]]]:
        """Partidas en bruto de los últimos `months` meses, del mes más reciente al más antiguo.

        Para en cuanto tiene `limit` partidas en las que juega ese usuario (aún sin filtrar variantes, por eso se
        piden algunas más). Devuelve también los meses que se han leído.
        """
        user = username.lower()
        deadline = self.clock() + TOTAL_TIMEOUT_S
        try:
            profile = self._get(f"{API_BASE}/player/{user}", deadline)
        except ChessComNotFound as exc:
            raise ChessComNotFound(f"No hay ningún usuario de Chess.com que se llame «{username}».") from exc
        if str(profile.get("status") or "").startswith("closed"):
            raise ChessComClosed(f"La cuenta «{username}» está cerrada en Chess.com y no se pueden leer sus partidas.")
        try:
            body = self._get(f"{API_BASE}/player/{user}/games/archives", deadline)
        except ChessComNotFound:
            return [], []
        prefix = f"{API_BASE}/player/{user}/games/"
        available: dict[tuple[int, int], str] = {}
        for url in body.get("archives") or []:
            if not isinstance(url, str) or not url.lower().startswith(prefix):
                continue
            m = re.fullmatch(r"(\d{4})/(\d{2})", url[len(prefix) :])
            if m:
                available[(int(m.group(1)), int(m.group(2)))] = prefix + m.group(0)
        games: list[dict[str, Any]] = []
        read: list[tuple[int, int]] = []
        for ym in recent_months(now, months):
            if ym not in available:
                continue
            try:
                month = self._get(available[ym], deadline)
            except ChessComNotFound:
                continue
            except _TooLarge as exc:
                if games:
                    break  # con lo que ya hay basta
                raise ChessComError(
                    "Ese mes tiene demasiadas partidas para traerlas aquí; prueba con otra cuenta."
                ) from exc
            except ChessComError:
                if games:
                    break  # un mes antiguo que falla no tira lo ya leído
                raise
            read.append(ym)
            batch = [g for g in month.get("games") or [] if isinstance(g, dict)]
            batch.sort(
                key=lambda g: g.get("end_time") if isinstance(g.get("end_time"), int | float) else 0, reverse=True
            )
            games.extend(batch)
            if len(games) >= limit * 2:
                break
        return games, read


def import_id(owner: str, game_url: str) -> str:
    """Id estable por persona y partida: traerla otra vez no la duplica."""
    return hashlib.sha256(f"chesscom:{owner}:{game_url}".encode()).hexdigest()[:16]


@dataclass
class Skipped:
    variantes: int = 0  # ajedrez 960, bughouse, posición inicial distinta...
    ajenas: int = 0  # el usuario no juega en ellas
    sin_jugadas: int = 0  # abandonadas antes de empezar o PGN ilegible


def _side(game: dict[str, Any], color: str) -> dict[str, Any]:
    side = game.get(color)
    return side if isinstance(side, dict) else {}


def _is_standard(game: dict[str, Any]) -> bool:
    if game.get("rules") != "chess":
        return False
    setup = game.get("initial_setup")
    return not setup or str(setup).split()[:4] == chess.STARTING_FEN.split()[:4]


def _clean_name(value: Any) -> str:
    name = str(value or "")
    return name if _USERNAME.match(name) else "rival"


def _moves(pgn_text: Any) -> list[str] | None:
    if not isinstance(pgn_text, str) or not pgn_text or len(pgn_text) > MAX_PGN_CHARS:
        return None
    try:
        parsed = chess.pgn.read_game(io.StringIO(pgn_text))
    except Exception:  # noqa: BLE001 - PGN roto
        return None
    if parsed is None or parsed.errors or parsed.board().fen() != chess.STARTING_FEN:
        return None
    return [m.uci() for m in parsed.mainline_moves()]


def _elo(side: dict[str, Any]) -> int | None:
    value = side.get("rating")
    return int(value) if isinstance(value, int | float) and 0 < value < 4000 else None


def _control(raw: Any) -> str | None:
    """«180+2» -> «3+2»; «600» -> «10 min»; «1/86400» -> «1 día por jugada»."""
    text = str(raw or "")
    m = re.fullmatch(r"(\d{1,6})(?:\+(\d{1,4}))?", text)
    if m:
        base, inc = int(m.group(1)), int(m.group(2) or 0)
        mins = f"{base // 60}" if base % 60 == 0 else f"{base / 60:.1f}".replace(".", ",")
        return f"{mins}+{inc}" if inc else f"{mins} min"
    m = re.fullmatch(r"1/(\d{1,7})", text)
    if m:
        days = max(1, int(m.group(1)) // 86400)
        return f"{days} {'día' if days == 1 else 'días'} por jugada"
    return None


def to_entry(game: dict[str, Any], username: str, owner: str) -> dict[str, Any] | str:
    """Lo que se guarda de una partida de Chess.com, o el motivo por el que no se importa ("variantes"...)."""
    if not _is_standard(game):
        return "variantes"
    white, black = _side(game, "white"), _side(game, "black")
    me = username.lower()
    if str(white.get("username", "")).lower() == me:
        color, mine, theirs = "white", white, black
    elif str(black.get("username", "")).lower() == me:
        color, mine, theirs = "black", black, white
    else:
        return "ajenas"
    url = str(game.get("url") or "")
    if not re.fullmatch(r"https://www\.chess\.com/game/(live|daily)/\d{1,15}", url):
        return "sin_jugadas"
    moves = _moves(game.get("pgn"))
    if not moves or len(moves) < 2:
        return "sin_jugadas"
    my_code, their_code = str(mine.get("result") or ""), str(theirs.get("result") or "")
    if my_code == "win":
        result, reason = "victoria", END_REASONS.get(their_code)
        if reason == "abandono":
            reason = "abandono del rival"
    elif my_code in DRAW_CODES or their_code in DRAW_CODES:
        result, reason = "tablas", END_REASONS.get(my_code if my_code in DRAW_CODES else their_code)
    elif their_code == "win":
        result, reason = "derrota", END_REASONS.get(my_code)
    else:
        return "sin_jugadas"
    white_won = (result == "victoria") == (color == "white")
    score = "1/2-1/2" if result == "tablas" else ("1-0" if white_won else "0-1")
    end_time = game.get("end_time")
    created = float(end_time) if isinstance(end_time, int | float) and end_time > 0 else 0.0
    time_class = str(game.get("time_class") or "")
    return {
        "id": import_id(owner, url),
        "created": created,
        "user_color": color,
        "rival": _clean_name(theirs.get("username")),
        "elo": _elo(mine),
        "rival_elo": _elo(theirs),
        "ritmo": TIME_CLASSES.get(time_class),
        "control": _control(game.get("time_control")),
        "url": url,
        "result": score,
        "resultado_usuario": result,
        "motivo_fin": reason,
        "moves": moves,
    }


def pick(games: list[dict[str, Any]], username: str, owner: str, limit: int) -> tuple[list[dict[str, Any]], Skipped]:
    """Las `limit` partidas más recientes que se pueden importar (sin repetir)."""
    skipped = Skipped()
    ready: dict[str, dict[str, Any]] = {}
    for raw in games:
        entry = to_entry(raw, username, owner)
        if isinstance(entry, str):
            setattr(skipped, entry, getattr(skipped, entry) + 1)
            continue
        ready.setdefault(entry["id"], entry)
    chosen = sorted(ready.values(), key=lambda g: g["created"], reverse=True)
    return chosen[:limit], skipped
