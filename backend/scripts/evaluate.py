"""Evaluación offline de Mea culpa.

1) Diagnóstico: sobre los PGN de data/pgn, ¿el error que el LLM señala como principal
   coincide con la mayor caída de evaluación del motor? ¿Su patrón está entre las etiquetas
   del motor? ¿Cuánto inventa (y se descarta)? Coste y latencia por análisis.
2) Jugadas ilegales: en posiciones de esas partidas, cuántas jugadas ilegales propone el
   modelo a cada temperatura, cuántas veces acierta a la primera y cuántos fallbacks hay.

Uso (desde la raíz del repo):
    .venv/Scripts/python backend/scripts/evaluate.py                 # mock
    LLM_PROVIDER=azure ... .venv/Scripts/python backend/scripts/evaluate.py --temps defecto 0 0.7

`--temps defecto` (lo predeterminado) no envía temperatura: se usa la del proveedor.

Escribe una tabla markdown en eval/resultados_<proveedor>.md y la imprime.
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

import chess  # noqa: E402

from meaculpa.analysis import (  # noqa: E402
    analyze_moves,
    diagnose,
    history_counts,
    make_analyzer,
    parse_pgn,
    pgn_without_comments,
)
from meaculpa.config import get_settings  # noqa: E402
from meaculpa.llm import get_provider  # noqa: E402
from meaculpa.llm_player import LLMOpponent  # noqa: E402


def evaluate_diagnosis(pgn_paths: list[Path], provider, settings) -> tuple[list[str], dict]:
    rows = []
    hits = pattern_hits = 0
    latencies, costs = [], []
    invented = 0
    previous: list[list[str]] = []
    for path in pgn_paths:
        game = parse_pgn(path.read_text(encoding="utf-8"))
        user = game.headers.get("MeaCulpaUser", "black")
        color = chess.WHITE if user == "white" else chess.BLACK
        analyzer = make_analyzer(settings)
        t0 = time.perf_counter()
        try:
            analysis = analyze_moves(list(game.mainline_moves()), color, analyzer, game.board().fen())
        finally:
            analyzer.close()
        engine_s = time.perf_counter() - t0
        history = history_counts(previous, analysis.patrones_partida)
        previous.append(analysis.patrones_partida)
        diag = diagnose(analysis, provider, pgn_without_comments(game), "derrota", history, settings=settings)
        worst = analysis.biggest_drop()
        if diag.diagnostico is None:
            rows.append(
                f"| {path.stem} | {worst.jugada if worst else '-'} | (error: {diag.error}) | ❌ | - | - | - | - |"
            )
            continue
        d = diag.diagnostico
        llm_ply = d["error_principal_ply"]
        hit = worst is not None and llm_ply == worst.ply and not diag.grounding["principal_corregido"]
        llm_err = next((e for e in d["errores"] if e["ply"] == llm_ply), None)
        pat_hit = bool(worst and llm_err and llm_err["patron"] in worst.etiquetas)
        hits += hit
        pattern_hits += pat_hit
        invented += diag.grounding["errores_inventados_descartados"]
        latencies.append(diag.latency_ms)
        costs.append(diag.coste_usd)
        llm_move = llm_err["jugada"] if llm_err else f"ply {llm_ply}"
        worst_txt = f"{worst.jugada_numero}. {worst.jugada} (−{worst.caida_cp} cp)" if worst else "-"
        rows.append(
            f"| {path.stem} | {worst_txt} | {llm_move} | {'✅' if hit else '❌'} | {'✅' if pat_hit else '❌'} | "
            f"{diag.grounding['errores_inventados_descartados']} | {engine_s:.1f} s | {diag.latency_ms:.0f} ms | "
            f"${diag.coste_usd:.5f} |"
        )
    n = len(pgn_paths)
    summary = {
        "coincidencia": hits / n if n else 0,
        "patron": pattern_hits / n if n else 0,
        "inventados": invented,
        "latencia_p50": statistics.median(latencies) if latencies else 0,
        "coste_medio": statistics.mean(costs) if costs else 0,
    }
    header = [
        "| Partida | Mayor caída (motor) | Error principal (LLM) | ¿Coincide? | ¿Patrón OK? | Inventados descartados | Motor | LLM | Coste |",
        "|---|---|---|:-:|:-:|:-:|--:|--:|--:|",
    ]
    return header + rows, summary


def sample_positions(pgn_paths: list[Path], every: int) -> list[chess.Board]:
    boards = []
    for path in pgn_paths:
        game = parse_pgn(path.read_text(encoding="utf-8"))
        board = game.board()
        for i, move in enumerate(game.mainline_moves()):
            if i % every == 0 and not board.is_game_over():
                boards.append(board.copy())
            board.push(move)
    return boards


def evaluate_illegal_moves(
    boards: list[chess.Board], provider, temps: list[float | None], max_retries: int
) -> list[str]:
    lines = [
        "| Modelo | Temperatura | Posiciones | Propuestas | Ilegales | Tasa ilegal | Legal a la 1ª | Fallbacks |",
        "|---|--:|--:|--:|--:|--:|--:|--:|",
    ]
    for t in temps:
        player = LLMOpponent(provider, temperature=t, max_retries=max_retries)
        proposals = illegal = first_ok = fallbacks = 0
        for b in boards:
            rec = player.play(b)
            proposals += len(rec.attempts)
            illegal += rec.illegal_count
            first_ok += rec.attempts[0].valida
            fallbacks += rec.fallback
        lines.append(
            f"| {provider.name} | {'defecto' if t is None else f'{t:.1f}'} | {len(boards)} | {proposals} | {illegal} | "
            f"{illegal / proposals:.1%} | {first_ok / len(boards):.1%} | {fallbacks} |"
        )
    return lines


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--pgn-dir", type=Path, default=ROOT / "data" / "pgn")
    parser.add_argument(
        "--temps",
        type=lambda v: None if v.lower() in ("defecto", "default", "none") else float(v),
        nargs="+",
        default=[None],
        help="temperaturas a probar; 'defecto' = no enviarla",
    )
    parser.add_argument("--every", type=int, default=4, help="una posición de cada N plies para el test de ilegales")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    settings = get_settings()
    provider = get_provider(settings)
    pgns = sorted(args.pgn_dir.glob("*.pgn"))
    diag_lines, summary = evaluate_diagnosis(pgns, provider, settings)
    boards = sample_positions(pgns, args.every)
    illegal_lines = evaluate_illegal_moves(boards, provider, args.temps, settings.llm_max_retries)

    analyzer = make_analyzer(settings)
    engine_name = analyzer.name
    analyzer.close()
    md = [
        f"# Evaluación de Mea culpa — proveedor `{provider.kind}` / modelo `{provider.name}`",
        "",
        f"Motor de análisis: {engine_name}. Partidas: {len(pgns)}. Fecha: {time.strftime('%Y-%m-%d %H:%M')}.",
        "",
    ]
    if provider.kind == "mock":
        md += [
            "> ⚠️ Proveedor **mock**: estas cifras validan el pipeline (motor → hechos → LLM → validación),",
            "> no la calidad de ningún modelo. El mock elige siempre la mayor caída, así que la coincidencia",
            "> es 100 % por construcción. Ejecuta con `LLM_PROVIDER=azure` para medir un modelo real.",
            "",
        ]
    md += [
        "## 1. ¿El LLM señala el mismo error que el motor?",
        "",
        *diag_lines,
        "",
        f"- Coincidencia error principal ↔ mayor caída del motor: **{summary['coincidencia']:.0%}**",
        f"- Patrón del error principal dentro de las etiquetas del motor: **{summary['patron']:.0%}**",
        f"- Errores inventados por el LLM (descartados por la validación): **{summary['inventados']}**",
        f"- Latencia LLM (mediana): **{summary['latencia_p50']:.0f} ms** · coste medio por análisis: "
        f"**${summary['coste_medio']:.5f}** (precios en `LLM_PRICE_*_PER_M`)",
        "",
        "## 2. Jugadas ilegales propuestas por el modelo",
        "",
        f"Posiciones: una de cada {args.every} plies de las partidas de muestra. "
        f"Reintentos máximos: {settings.llm_max_retries}.",
        "",
        *illegal_lines,
        "",
    ]
    text = "\n".join(md)
    out = args.out or ROOT / "eval" / f"resultados_{provider.kind}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    print(text)
    print(f"\n(guardado en {out.relative_to(ROOT) if out.is_relative_to(ROOT) else out})")


if __name__ == "__main__":
    main()
