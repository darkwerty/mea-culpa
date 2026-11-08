"""Torneo de punta a punta sin red: mock LLM en modo experimento, motores clásicos y Stockfish falso."""

from __future__ import annotations

import json
from pathlib import Path

import chess
import chess.engine
import chess.pgn
import pytest

from meaculpa.experiment import load_config, run_experiment, schedule
from meaculpa.openings import extract_start_positions, read_games

GAMES = [
    ("qwerty0x", "RivalSecretoA", "1. e4 b6 2. d4 Bb7 3. Nf3 Bxe4 4. Nc3 Bb7 5. Bf4 d6 6. d5 Nf6"),
    ("RivalSecretoB", "qwerty0x", "1. d4 d5 2. c4 e6 3. Nc3 Nf6 4. Bg5 Be7 5. e3 O-O 6. Nf3 h6"),
    ("qwerty0x", "RivalSecretoC", "1. e4 c5 2. Nf3 d6 3. d4 cxd4 4. Nxd4 Nf6 5. Nc3 a6 6. Be2 e5"),
]


def _pgn_text() -> str:
    out = []
    for i, (w, b, moves) in enumerate(GAMES):
        out.append(
            f'[Event "Live"]\n[Date "2026.10.0{i + 1}"]\n[White "{w}"]\n[Black "{b}"]\n[Result "*"]\n'
            f'[WhiteElo "1100"]\n[BlackElo "1150"]\n\n{moves} *\n'
        )
    return "\n".join(out)


class FakeEngine:
    def __init__(self, path: str) -> None:
        self.options: dict = {}

    def configure(self, options: dict) -> None:
        self.options.update(options)

    def play(self, board: chess.Board, limit):
        return chess.engine.PlayResult(sorted(board.legal_moves, key=lambda m: m.uci())[0], None)

    def quit(self) -> None:
        pass


@pytest.fixture
def cfg(tmp_path):
    pgn = tmp_path / "partidas.pgn"
    pgn.write_text(_pgn_text(), encoding="utf-8")
    return {
        "nombre": "test",
        "semilla": 1,
        "partidas_por_emparejamiento": 4,
        "tope_plies": 30,
        "presupuesto_usd": 5.0,
        "bootstrap": 50,
        "stockfish": "fake-stockfish",
        "aperturas": {"pgn": str(pgn), "usuario": "qwerty0x", "plies_min": 4, "plies_max": 8, "ambos_colores": True},
        "llm": {"max_reintentos": 3},
        "anclas": [
            {"nombre": "aleatorio", "tipo": "aleatorio", "elo": 400},
            {"nombre": "sf", "tipo": "stockfish", "elo": 1320, "uci_elo": 1320, "nodos": 100},
        ],
        "participantes": [
            {"nombre": "mock", "tipo": "mock", "semilla": 3, "precio_entrada_1m": 1.0, "precio_salida_1m": 2.0},
            {"nombre": "codicioso", "tipo": "codicioso"},
        ],
    }


def test_full_tournament_writes_pgn_jsonl_and_report(cfg, tmp_path):
    out = tmp_path / "salida"
    res = run_experiment(cfg, out, engine_factory=FakeEngine, log=lambda _: None)
    assert res.stopped is None
    rows = [json.loads(line) for line in (out / "partidas.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 2 * 2 * 4  # participantes × anclas × partidas
    for r in rows:
        assert r["resultado"] in ("1-0", "0-1", "1/2-1/2")
        assert r["plies"] <= 30
        assert r["puntos_jugador"] in (0.0, 0.5, 1.0)
        if r["terminacion"] == "tope_plies":
            assert r["resultado"] == "1/2-1/2"
    # Colores alternos y cada apertura con los dos colores.
    mock_vs_sf = sorted((r for r in rows if r["id"].startswith("mock__sf__")), key=lambda r: r["ronda"])
    assert [r["color_jugador"] for r in mock_vs_sf] == ["blancas", "negras", "blancas", "negras"]
    assert mock_vs_sf[0]["apertura"] == mock_vs_sf[1]["apertura"] != mock_vs_sf[2]["apertura"]
    # PGN por partida y combinado, desde la posición inicial con la apertura real.
    pgn_files = list((out / "pgn").glob("*.pgn"))
    assert len(pgn_files) == len(rows)
    text = (out / "partidas.pgn").read_text(encoding="utf-8")
    assert text.count("[Event ") == len(rows)
    assert "RivalSecreto" not in text and "rival (Elo" in text
    with open(pgn_files[0], encoding="utf-8") as fh:
        game = chess.pgn.read_game(fh)
    assert game.headers["MeaCulpaPlyInicio"].isdigit()
    # Métricas por jugada del LLM con coste.
    moves = [json.loads(line) for line in (out / "jugadas_llm.jsonl").read_text(encoding="utf-8").splitlines()]
    assert moves and all(m["jugador"] == "mock" and m["cost_usd"] > 0 and "partida" in m for m in moves)
    assert res.spent_usd == pytest.approx(sum(m["cost_usd"] for m in moves), rel=1e-6)
    # Informe con Elo de los participantes.
    report = res.report_path.read_text(encoding="utf-8")
    assert "| mock |" in report and "| codicioso |" in report and "IC 95 %" in report
    assert set(res.estimates) == {"mock", "codicioso"}


def test_budget_stops_experiment_and_resume_skips_finished(cfg, tmp_path):
    cfg["participantes"] = [cfg["participantes"][0]]
    cfg["anclas"] = [cfg["anclas"][0]]
    cfg["participantes"][0]["precio_entrada_1m"] = 50.0  # ~0,01 USD por llamada
    cfg["presupuesto_usd"] = 0.2
    out = tmp_path / "s"
    res = run_experiment(cfg, out, engine_factory=FakeEngine, log=lambda _: None)
    assert res.stopped == "presupuesto agotado"
    assert res.spent_usd <= 0.2  # tope duro: nunca se pasa
    rows = [json.loads(line) for line in (out / "partidas.jsonl").read_text(encoding="utf-8").splitlines()]
    assert rows[-1]["resultado"] == "*" and rows[-1]["terminacion"] == "presupuesto"

    # Relanzar con más presupuesto: no repite las terminadas y suma el gasto previo.
    cfg["presupuesto_usd"] = 100.0
    res2 = run_experiment(cfg, out, engine_factory=FakeEngine, log=lambda _: None)
    finals = {r["id"]: r for r in res2.rows}
    assert len(finals) == 4 and all(r["resultado"] != "*" for r in finals.values())
    done_first = [r["id"] for r in rows if r["resultado"] != "*"]
    assert sum(1 for r in res2.rows if r["id"] in done_first) == len(done_first)  # no se rejugaron
    assert res2.spent_usd >= res.spent_usd


def test_llm_forfeit_counts_as_loss(cfg, tmp_path):
    cfg["participantes"] = [{"nombre": "malo", "tipo": "mock", "ilegal_base": 1.0}]
    cfg["anclas"] = [cfg["anclas"][0]]
    cfg["llm"] = {"max_reintentos": 0}
    cfg["partidas_por_emparejamiento"] = 2
    res = run_experiment(cfg, tmp_path / "o", engine_factory=FakeEngine, log=lambda _: None)
    assert all(r["terminacion"] == "ilegal" and r["puntos_jugador"] == 0.0 for r in res.rows)
    assert res.estimates["malo"].rating is None  # 0 %: no se publica Elo
    assert "sin estimar (0 %)" in res.report_path.read_text(encoding="utf-8")


def test_api_error_voids_game_instead_of_draw(cfg, tmp_path):
    class Broken:
        name, kind, supports_structured = "roto", "test", True

        def structured(self, *a, **k):
            raise ConnectionError("sin conexión")

    cfg["participantes"] = [{"nombre": "roto", "tipo": "mock"}]
    cfg["anclas"] = [cfg["anclas"][0]]
    res = run_experiment(cfg, tmp_path / "e", provider_factory=lambda spec: Broken(), log=lambda _: None)
    assert res.stopped == "3 errores de API seguidos"
    assert all(r["resultado"] == "*" and r["terminacion"] == "error_api" for r in res.rows)
    assert "roto" not in res.estimates


def test_yaml_config_with_env_vars_and_schedule(cfg, tmp_path, monkeypatch):
    monkeypatch.setenv("MI_PGN", cfg["aperturas"]["pgn"])
    path = tmp_path / "exp.yaml"
    path.write_text(
        "nombre: y\nmodo: todos\npartidas_por_emparejamiento: 2\naperturas: {pgn: '${MI_PGN}'}\n"
        "anclas: [{nombre: a, tipo: aleatorio, elo: 400}]\n"
        "participantes: [{nombre: p1, tipo: codicioso}, {nombre: p2, tipo: minimax, profundidad: 1}]\n",
        encoding="utf-8",
    )
    loaded = load_config(path)
    assert loaded["aperturas"]["pgn"] == cfg["aperturas"]["pgn"]
    positions = extract_start_positions(read_games(Path(cfg["aperturas"]["pgn"])))
    games = schedule(loaded, positions)
    assert {(g.a["nombre"], g.b["nombre"]) for g in games} == {("p1", "a"), ("p2", "a"), ("p1", "p2")}
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"anclas": [{"nombre": "a", "tipo": "aleatorio"}], "participantes": []}))
    with pytest.raises(ValueError):
        load_config(bad)
