"""Escalera de rivales: cada participante empieza contra el ancla más débil y sube cuando puntúa bastante.

Usa `meaculpa.experiment.run_experiment` por bloques (una carpeta por participante, así que relanzar
continúa donde se quedó). Reglas:
- Bloques de `escalera.bloque` partidas (4 = 2 posiciones x 2 colores) contra el peldaño actual.
- Tras cada bloque, si la puntuación acumulada contra ese peldaño es >= `escalera.subir_si` (0,65),
  se sube al siguiente; si no, se sigue en él. Para el participante a las `escalera.partidas` partidas,
  al pasar `escalera.minutos_por_participante` o si el experimento se detiene (errores de API, gasto).
- `presupuesto_usd` es el tope DURO de todo el fichero (todos los participantes juntos).
- Al final escribe `resultados.md` con el Elo de todos contra todos los peldaños (Elo fijo).

Uso (desde la raíz del repo):
    .venv/Scripts/python backend/scripts/escalera.py eval/experimentos/locales.yaml [--solo nombre ...]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from meaculpa.experiment import (  # noqa: E402
    _final_rows,
    _read_jsonl,
    estimate_from_rows,
    load_config,
    render_report,
    run_experiment,
)


def _score(rows: list[dict], player: str, rival: str) -> tuple[int, float]:
    valid = [r for r in _final_rows(rows) if r["jugador"] == player and r["rival"] == rival and r["resultado"] != "*"]
    return len(valid), sum(r["puntos_jugador"] for r in valid)


def _spent(out: Path, names: list[str]) -> float:
    return sum(float(r.get("coste_usd") or 0.0) for n in names for r in _read_jsonl(out / n / "partidas.jsonl"))


def climb(cfg: dict, spec: dict, out: Path, all_names: list[str]) -> str:
    ladder_cfg = cfg.get("escalera") or {}
    block = int(ladder_cfg.get("bloque", 4))
    total = int(ladder_cfg.get("partidas", 20))
    up = float(ladder_cfg.get("subir_si", 0.65))
    minutes = float(ladder_cfg.get("minutos_por_participante", 1e9))
    rungs = cfg["peldanos"]
    name = spec["nombre"]
    mine = out / name
    rows = _read_jsonl(mine / "partidas.jsonl")
    # Al relanzar: seguir en el peldaño más alto ya jugado.
    rung = max((i for i, a in enumerate(rungs) if _score(rows, name, a["nombre"])[0]), default=0)
    t0 = time.monotonic()
    while True:
        valid = [r for r in _final_rows(rows) if r["resultado"] != "*"]
        if len(valid) >= total:
            return f"{len(valid)} partidas"
        if time.monotonic() - t0 > minutes * 60:
            return f"tiempo agotado ({minutes:g} min)"
        anchor = rungs[rung]
        budget = cfg.get("presupuesto_usd")
        stage = {
            **cfg,
            "nombre": f"{cfg.get('nombre', 'escalera')} · {name}",
            "modo": "anclas",
            "participantes": [spec],
            "anclas": [anchor],
            "partidas_por_emparejamiento": total,
        }
        if budget is not None:  # tope global: lo que queda + lo ya gastado en esta carpeta
            stage["presupuesto_usd"] = float(budget) - _spent(out, all_names) + _spent(out, [name])
        res = run_experiment(stage, mine, max_games=min(block, total - len(valid)))
        rows = _read_jsonl(mine / "partidas.jsonl")
        if res.stopped:
            return f"detenido: {res.stopped}"
        n, pts = _score(rows, name, anchor["nombre"])
        print(f"== {name} contra {anchor['nombre']}: {pts:g}/{n}")
        if n and n % block == 0 and pts / n >= up and rung + 1 < len(rungs):
            rung += 1
            print(f"== {name} sube a {rungs[rung]['nombre']}")
        elif n >= total and len([r for r in _final_rows(rows) if r["resultado"] != "*"]) < total:
            # ya no quedan aperturas contra este peldaño (y aún faltan partidas)
            return "sin aperturas nuevas en este peldaño"


def final_report(cfg: dict, out: Path, specs: list[dict], notes: dict[str, str]) -> Path:
    full = {**cfg, "anclas": cfg["peldanos"], "participantes": specs, "modo": "anclas"}
    rows = [r for s in specs for r in _read_jsonl(out / s["nombre"] / "partidas.jsonl")]
    est = estimate_from_rows(rows, full)
    spent = sum(float(r.get("coste_usd") or 0.0) for r in rows)
    report = render_report(full, rows, est, spent, None)
    if notes:
        report += "\n## Cómo terminó cada escalera\n\n" + "".join(f"- {k}: {v}\n" for k, v in notes.items())
    path = out / "resultados.md"
    path.write_text(report, encoding="utf-8")
    (out / "estimaciones.json").write_text(
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
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("config", type=Path)
    parser.add_argument("--solo", nargs="*", default=None, help="jugar solo estos participantes")
    parser.add_argument("--informe", action="store_true", help="solo rehacer el informe conjunto")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    cfg = load_config(args.config)
    if not cfg.get("peldanos"):
        raise SystemExit("Falta 'peldanos': anclas de Elo fijo, de la más débil a la más fuerte")
    out = Path(cfg.get("salida") or ROOT / "eval" / "experimento" / cfg.get("nombre", "escalera"))
    if not out.is_absolute():
        out = ROOT / out
    specs = cfg["participantes"]
    names = [s["nombre"] for s in specs]
    notes_path = out / "escalera.json"
    notes = json.loads(notes_path.read_text(encoding="utf-8")) if notes_path.exists() else {}
    if not args.informe:
        for spec in specs:
            if args.solo is not None and spec["nombre"] not in args.solo:
                continue
            print(f"\n######## {spec['nombre']}")
            notes[spec["nombre"]] = climb(cfg, spec, out, names)
            print(f"######## {spec['nombre']}: {notes[spec['nombre']]}")
            out.mkdir(parents=True, exist_ok=True)
            notes_path.write_text(json.dumps(notes, ensure_ascii=False, indent=2), encoding="utf-8")
    path = final_report(cfg, out, specs, notes)
    print(path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
