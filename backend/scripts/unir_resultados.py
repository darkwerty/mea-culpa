"""Une varias carpetas de resultados del mismo torneo (p. ej. dos tandas con distinta semilla) y rehace el informe.

Uso (desde la raíz del repo):
    .venv/Scripts/python backend/scripts/unir_resultados.py eval/experimentos/calibracion.yaml \
        eval/experimento/calibracion eval/experimento/calibracion_b --salida eval/experimento/calibracion_total

La configuración da las anclas (Elo fijo) y los participantes; las partidas salen de los `partidas.jsonl`
de cada carpeta.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from meaculpa.experiment import _read_jsonl, estimate_from_rows, load_config, render_report  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("config", type=Path)
    parser.add_argument("carpetas", type=Path, nargs="+")
    parser.add_argument("--salida", type=Path, required=True)
    parser.add_argument("--nombre", default=None)
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    cfg = load_config(args.config)
    if args.nombre:
        cfg["nombre"] = args.nombre
    # Las tandas repiten identificadores de partida: se distinguen anteponiendo la carpeta.
    rows = [{**r, "id": f"{d.name}/{r['id']}"} for d in args.carpetas for r in _read_jsonl(d / "partidas.jsonl")]
    est = estimate_from_rows(rows, cfg)
    spent = sum(float(r.get("coste_usd") or 0.0) for r in rows)
    report = render_report(cfg, rows, est, spent, None)
    report += "\nTandas unidas: " + ", ".join(f"`{d.as_posix()}`" for d in args.carpetas) + ".\n"
    args.salida.mkdir(parents=True, exist_ok=True)
    (args.salida / "resultados.md").write_text(report, encoding="utf-8")
    (args.salida / "estimaciones.json").write_text(
        json.dumps(
            {
                k: {"partidas": e.games, "puntos": e.score, "elo": e.rating, "ic95": [e.ci_low, e.ci_high]}
                for k, e in est.items()
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(report)


if __name__ == "__main__":
    main()
