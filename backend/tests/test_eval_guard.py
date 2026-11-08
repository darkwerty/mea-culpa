"""/eval no puntúa jugadas candidatas en una partida que cuenta para el ranking, y la partida en curso se retoma."""

from __future__ import annotations

import threading
import time

import chess
from conftest import auth, fake_verifier
from fastapi.testclient import TestClient
from test_live_api import FakeEngine

from meaculpa.eval_guard import BLOCKED_MESSAGE, EvalGuard, position_key
from meaculpa.games import GameRecord, GameStore
from meaculpa.llm import MockProvider
from meaculpa.main import create_app
from meaculpa.storage import FileObjectStore

API = "/api/meaculpa"


def _client(settings, *, provider=None, user="ana") -> TestClient:
    app = create_app(
        settings=settings,
        provider=provider or MockProvider(seed=5),
        token_verifier=fake_verifier,
        live_engine=FakeEngine(),
    )
    return TestClient(app, headers=auth(user) if user else None)


def _new_game(client, color="white", opponent="random"):
    res = client.post(f"{API}/games", json={"opponent": {"type": opponent}, "user_color": color})
    assert res.status_code == 200, res.text
    return res.json()


def _play(client, game):
    """Una jugada legal del jugador (la primera por orden UCI)."""
    res = client.post(f"{API}/games/{game['id']}/move", json={"uci": sorted(game["legal_moves"])[0]})
    assert res.status_code == 200, res.text
    return res.json()


def _candidate(game) -> str:
    """FEN tras una jugada que el jugador podría hacer pero no ha hecho: lo que no debe poder puntuar."""
    board = chess.Board(game["fen"])
    board.push(max(board.legal_moves, key=lambda m: m.uci()))
    return board.fen()


def _eval(client, fen, game_id=None):
    body = {"fen": fen} | ({"game_id": game_id} if game_id else {})
    return client.post(f"{API}/eval", json=body)


def test_ranked_game_only_evaluates_its_own_positions(settings):
    client = _client(settings)
    game = _play(client, _new_game(client))
    assert game["status"] == "en_curso" and not game["con_ayuda"]
    assert _eval(client, game["fen"], game["id"]).status_code == 200
    assert _eval(client, chess.STARTING_FEN, game["id"]).status_code == 200  # posición anterior
    after_first = chess.Board()
    after_first.push_uci(game["moves_uci"][0])
    assert _eval(client, after_first.fen(), game["id"]).status_code == 200
    # Una jugada candidata no jugada: con game_id y sin él.
    for gid in (game["id"], None):
        res = _eval(client, _candidate(game), gid)
        assert res.status_code == 409
        assert res.json()["detail"] == BLOCKED_MESSAGE
    # Tampoco una posición cualquiera, ni con el id de otra cosa.
    other = "4k3/8/8/8/8/8/4P3/4K3 w - - 0 1"
    assert _eval(client, other).status_code == 409
    assert _eval(client, other, "abcdef123456").status_code == 409
    # A otra persona no le afecta.
    assert client.post(f"{API}/eval", json={"fen": other}, headers=auth("bea")).status_code == 200


def test_candidate_positions_are_free_once_the_game_has_help_or_is_over(settings):
    client = _client(settings)
    game = _play(client, _new_game(client))
    assert _eval(client, _candidate(game), game["id"]).status_code == 409
    assert client.post(f"{API}/games/{game['id']}/hint").status_code == 200  # ya es «con ayuda»
    assert _eval(client, _candidate(game), game["id"]).status_code == 200

    ranked = _play(client, _new_game(client))
    assert _eval(client, _candidate(ranked)).status_code == 409
    client.post(f"{API}/games/{ranked['id']}/resign")
    assert _eval(client, _candidate(ranked)).status_code == 200


def test_reviewing_a_finished_game_while_another_counts(settings):
    client = _client(settings)
    done = _play(client, _new_game(client))
    done = client.post(f"{API}/games/{done['id']}/resign").json()
    live = _play(client, _new_game(client))
    # Las posiciones de la partida terminada se repasan con su id...
    assert _eval(client, done["fen"], done["id"]).status_code == 200
    # ...pero su id no abre la puerta a otras posiciones mientras la otra cuenta.
    assert _eval(client, _candidate(live), done["id"]).status_code == 409
    assert _eval(client, _candidate(done), done["id"]).status_code == 409


def test_guard_finds_a_game_started_on_another_replica(settings):
    first = _client(settings)
    game = _play(first, _new_game(first))
    second = _client(settings)  # otra réplica: misma partida en el almacén, memoria vacía
    assert _eval(second, _candidate(game)).status_code == 409
    assert _eval(second, game["fen"]).status_code == 200


def test_guard_rereads_a_game_that_moved_on_another_replica(settings):
    first = _client(settings)
    second = _client(settings)
    game = _new_game(first)
    assert _eval(second, game["fen"], game["id"]).status_code == 200  # la segunda réplica la conoce
    game = _play(first, game)  # se juega en la primera
    assert _eval(second, game["fen"], game["id"]).status_code == 200  # la relee antes de rechazar


def test_guard_unit_scan_is_cached(tmp_path):
    store = GameStore(FileObjectStore(tmp_path))
    owner = "a" * 64
    reads = []
    original = store.recent

    def counting(o, limit=50):
        reads.append(o)
        return original(o, limit=limit)

    store.recent = counting  # type: ignore[method-assign]
    now = [0.0]
    guard = EvalGuard(store, ttl_s=60, clock=lambda: now[0])
    game = GameRecord(id="0123456789ab", created=time.time(), user_color="white", opponent={}, owner=owner)
    store.save(game)
    board = chess.Board()
    guard.check(owner, board)
    guard.check(owner, board)
    assert len(reads) == 1
    now[0] = 61
    board.push_uci("e2e4")
    try:
        guard.check(owner, board)
    except Exception as exc:  # noqa: BLE001
        assert type(exc).__name__ == "EvalBlocked"
    else:
        raise AssertionError("debería rechazar una posición que no es de la partida")
    assert len(reads) == 2


def test_position_key_ignores_move_counters_and_impossible_en_passant():
    a = chess.Board("rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq e3 0 1")
    b = chess.Board("rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 4 9")
    assert position_key(a) == position_key(b)


# ------------------------------------------------------------------ retomar al recargar
def test_active_game_is_returned_until_it_ends(settings):
    client = _client(settings)
    assert client.get(f"{API}/games/activa").json() == {"partida": None}
    game = _play(client, _new_game(client))
    found = client.get(f"{API}/games/activa").json()["partida"]
    assert found["id"] == game["id"] and found["fen"] == game["fen"] and found["procesando"] is False
    assert found["moves_uci"] == game["moves_uci"]
    assert client.get(f"{API}/games/activa", headers=auth("bea")).json() == {"partida": None}
    client.post(f"{API}/games/{game['id']}/resign")
    assert client.get(f"{API}/games/activa").json() == {"partida": None}


def test_reloading_does_not_replay_the_rival_or_undo(settings):
    client = _client(settings)
    game = _play(client, _new_game(client, opponent="random"))
    again = client.get(f"{API}/games/{game['id']}").json()
    assert again["moves_uci"] == game["moves_uci"] and again["fen"] == game["fen"]
    assert client.get(f"{API}/games/activa").json()["partida"]["moves_uci"] == game["moves_uci"]


class _SlowProvider(MockProvider):
    """El rival IA se queda pensando hasta que el test lo suelta."""

    def __init__(self) -> None:
        super().__init__(seed=3, illegal_base=0.0)
        self.release = threading.Event()
        self.waiting = threading.Event()

    def structured(self, messages, schema, temperature=None):
        self.waiting.set()
        self.release.wait(10)
        return super().structured(messages, schema, temperature)


def test_a_move_in_flight_shows_as_processing_and_blocks_another(settings):
    provider = _SlowProvider()
    client = _client(settings, provider=provider)
    game = _new_game(client, opponent="llm")
    results = {}

    def move():
        results["first"] = client.post(f"{API}/games/{game['id']}/move", json={"uci": "e2e4"})

    t = threading.Thread(target=move)
    t.start()
    assert provider.waiting.wait(5)
    # Recargar a mitad: la partida se ve «procesando» y otra jugada no entra.
    state = client.get(f"{API}/games/{game['id']}").json()
    assert state["procesando"] is True and state["moves_uci"] == []
    second = client.post(f"{API}/games/{game['id']}/move", json={"uci": "d2d4"})
    assert second.status_code == 409
    provider.release.set()
    t.join(10)
    assert results["first"].status_code == 200
    final = client.get(f"{API}/games/{game['id']}").json()
    assert final["procesando"] is False and final["moves_uci"][0] == "e2e4" and len(final["moves_uci"]) == 2
