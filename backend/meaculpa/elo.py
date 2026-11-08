"""Elo por máxima verosimilitud contra anclas de Elo fijo, con IC 95 % por bootstrap.

Modelo logístico de Elo: E(A vence a B) = 1 / (1 + 10^((R_B − R_A)/400)). Las tablas cuentan como
medio punto (verosimilitud s·log E + (1−s)·log(1−E)). Las anclas tienen el Elo fijo; el resto se estima.
Para cada jugador libre la condición de máximo es Σ E = Σ s (puntos esperados = puntos obtenidos), que
se resuelve con Newton protegido por bisección; si hay partidas entre jugadores libres se itera
(Gauss-Seidel) hasta converger.

Con 0 % o 100 % de puntos no hay máximo finito: el Elo puntual es None (no se publica, como en LLM Chess).
El bootstrap remuestrea las partidas con reemplazo dentro de cada emparejamiento; las réplicas sin
máximo finito quedan en los extremos del dominio [lo, hi], y el IC lo refleja.
"""

from __future__ import annotations

import math
import random
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

LN10_400 = math.log(10) / 400

Game = tuple[str, str, float]  # (jugador A, jugador B, puntos de A: 1, 0,5 o 0)


def expected(ra: float, rb: float) -> float:
    return 1.0 / (1.0 + 10 ** ((rb - ra) / 400))


@dataclass
class EloEstimate:
    player: str
    games: int
    score: float  # puntos obtenidos
    rating: float | None  # None si 0 % o 100 % (sin máximo finito)
    ci_low: float | None = None
    ci_high: float | None = None

    @property
    def score_pct(self) -> float:
        return self.score / self.games if self.games else 0.0


def _opponents(games: Iterable[Game]) -> dict[str, dict[str, list[float]]]:
    """jugador → rival → [n partidas, puntos]."""
    table: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(lambda: [0.0, 0.0]))
    for a, b, s in games:
        if a == b:
            continue
        table[a][b][0] += 1
        table[a][b][1] += s
        table[b][a][0] += 1
        table[b][a][1] += 1 - s
    return table


def _solve(opps: dict[str, list[float]], ratings: dict[str, float], lo: float, hi: float, start: float) -> float:
    """R tal que Σ n·E(R, R_rival) = Σ puntos, con Newton dentro de un intervalo de bisección."""
    n_total = sum(n for n, _ in opps.values())
    score = sum(s for _, s in opps.values())
    if score <= 0:
        return lo
    if score >= n_total:
        return hi
    a, b = lo, hi
    r = min(max(start, lo), hi)
    for _ in range(100):
        f = 0.0
        df = 0.0
        for opp, (n, _) in opps.items():
            e = expected(r, ratings[opp])
            f += n * e
            df += n * e * (1 - e) * LN10_400
        f -= score
        if abs(f) < 1e-9:
            break
        if f > 0:
            b = r
        else:
            a = r
        step = r - f / df if df > 1e-12 else (a + b) / 2
        r = step if a < step < b else (a + b) / 2
        if b - a < 1e-6:
            break
    return r


def fit_ratings(
    games: Sequence[Game],
    anchors: dict[str, float],
    *,
    lo: float = -1000.0,
    hi: float = 4000.0,
    max_sweeps: int = 500,
    tol: float = 1e-4,
) -> dict[str, float]:
    """Elo de máxima verosimilitud de los jugadores no anclados (valores censurados en lo/hi)."""
    table = _opponents(games)
    free = sorted(p for p in table if p not in anchors)
    if not free:
        return {}
    start = sum(anchors.values()) / len(anchors) if anchors else 1500.0
    ratings: dict[str, float] = {**dict.fromkeys(free, start), **anchors}
    coupled = any(o not in anchors for p in free for o in table[p])
    for _ in range(max_sweeps if coupled else 1):
        delta = 0.0
        for p in free:
            new = _solve(table[p], ratings, lo, hi, ratings[p])
            delta = max(delta, abs(new - ratings[p]))
            ratings[p] = new
        if delta < tol:
            break
    return {p: ratings[p] for p in free}


def estimate(
    games: Sequence[Game],
    anchors: dict[str, float],
    *,
    bootstrap: int = 1000,
    seed: int = 0,
    alpha: float = 0.05,
    lo: float = -1000.0,
    hi: float = 4000.0,
) -> dict[str, EloEstimate]:
    """Elo puntual + IC (1−alpha) por bootstrap estratificado por emparejamiento."""
    table = _opponents(games)
    point = fit_ratings(games, anchors, lo=lo, hi=hi)
    out: dict[str, EloEstimate] = {}
    for p, rating in point.items():
        n = int(sum(v[0] for v in table[p].values()))
        s = sum(v[1] for v in table[p].values())
        censored = s <= 0 or s >= n
        out[p] = EloEstimate(p, n, s, None if censored else rating)
    if bootstrap <= 0 or not out:
        return out

    strata: dict[tuple[str, str], list[Game]] = defaultdict(list)
    for g in games:
        strata[(min(g[0], g[1]), max(g[0], g[1]))].append(g)
    rng = random.Random(seed)
    samples: dict[str, list[float]] = defaultdict(list)
    for _ in range(bootstrap):
        resample: list[Game] = []
        for group in strata.values():
            resample.extend(rng.choices(group, k=len(group)))
        for p, r in fit_ratings(resample, anchors, lo=lo, hi=hi).items():
            samples[p].append(r)
    for p, est in out.items():
        vals = sorted(samples[p])
        if vals:
            est.ci_low = _percentile(vals, alpha / 2)
            est.ci_high = _percentile(vals, 1 - alpha / 2)
    return out


def _percentile(sorted_vals: list[float], q: float) -> float:
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    pos = q * (len(sorted_vals) - 1)
    i = int(math.floor(pos))
    j = min(i + 1, len(sorted_vals) - 1)
    return sorted_vals[i] + (sorted_vals[j] - sorted_vals[i]) * (pos - i)
