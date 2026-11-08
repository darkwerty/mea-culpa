"""Torneo de calibración de Elo: participantes (motores, deployments de Foundry, Claude, Ollama) contra anclas.

Uso (desde la raíz del repo):
    .venv/Scripts/python backend/scripts/experiment.py eval/experimentos/ejemplo.yaml --dry-run
    .venv/Scripts/python backend/scripts/experiment.py eval/experimentos/ejemplo.yaml
    .venv/Scripts/python backend/scripts/experiment.py config.json --salida eval/experimentos/x --max-partidas 4

`--dry-run` no llama a ningún modelo: enseña el calendario, las aperturas y el peor caso de gasto por llamada.
Relanzar con la misma salida continúa donde se quedó (las partidas terminadas no se repiten).
Ver `meaculpa/experiment.py` para las reglas y el formato de salida.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from meaculpa.engines import find_stockfish  # noqa: E402
from meaculpa.experiment import LLM_TYPES, load_config, load_openings, run_experiment, schedule  # noqa: E402


def dry_run(cfg: dict) -> None:
    n = int(cfg.get("partidas_por_emparejamiento", 10))
    both = bool((cfg.get("aperturas") or {}).get("ambos_colores", True))
    positions = load_openings(cfg, -(-n // 2) if both else n, cfg.get("stockfish") or find_stockfish())
    games = schedule(cfg, positions)
    print(f"{len(games)} partidas · {len(positions)} posiciones iniciales · tope {cfg.get('tope_plies', 300)} plies")
    for pair, count in Counter((g.a["nombre"], g.b["nombre"]) for g in games).items():
        print(f"  {pair[0]} vs {pair[1]}: {count}")
    print("Primeras aperturas:")
    for p in positions[:5]:
        print(f"  {p.id}: {' '.join(p.san)}  ({p.origen})")
    print("Peor caso por llamada (≈700 tokens de entrada + max_tokens de salida):")
    for spec in cfg["participantes"]:
        if spec["tipo"] in LLM_TYPES:
            pin = float(spec.get("precio_entrada_1m", 0.0))
            pout = float(spec.get("precio_salida_1m", 0.0))
            mt = spec.get("max_tokens") or 4096
            worst = (700 * pin + mt * pout) / 1e6
            print(f"  {spec['nombre']}: {worst:.5f} USD (max_tokens={mt})")
    budget = cfg.get("presupuesto_usd")
    print(f"Tope de gasto: {budget if budget is not None else 'sin tope'} USD")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("config", type=Path, help="YAML o JSON del experimento")
    parser.add_argument("--salida", type=Path, default=None, help="carpeta de resultados (por defecto, la del config)")
    parser.add_argument("--max-partidas", type=int, default=None, help="jugar como mucho N partidas en esta ejecución")
    parser.add_argument("--dry-run", action="store_true", help="solo calendario y estimación; sin llamadas")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    cfg = load_config(args.config)
    if args.dry_run:
        dry_run(cfg)
        return
    out = args.salida or Path(cfg.get("salida") or ROOT / "eval" / "experimentos" / cfg.get("nombre", "experimento"))
    if not out.is_absolute():
        out = ROOT / out
    result = run_experiment(cfg, out, max_games=args.max_partidas)
    print()
    print(result.report_path.read_text(encoding="utf-8"))
    print(f"(resultados en {result.report_path.parent})")
    if result.stopped:
        print(f"Detenido: {result.stopped}")


if __name__ == "__main__":
    main()
