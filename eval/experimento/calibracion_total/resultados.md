# Experimento «calibracion_total»

Fecha: 2026-11-08 16:58. Partidas válidas: **1440** (anuladas: 0). Gasto en modelos: **0.0000 USD** de un tope de 0.00 USD.

Reglas: posiciones iniciales tras 4–8 plies de partidas amateur reales (sin repetir, cada una con los dos colores); fin por `board.outcome(claim_draw=True)` o tope de 300 plies (tablas, anotadas aparte); un LLM que agota 3 reintentos pierde por ilegalidad; los errores de API anulan la partida (no cuentan).

## Jugadores

| Jugador | Configuración | Elo fijo |
|---|---|--:|
| sf1320 | stockfish (elo=1320, uci_elo=1320, tiempo=0.1, hash=16) | 1320 |
| sf1468 | stockfish (elo=1468, uci_elo=1468, tiempo=0.1, hash=16) | 1468 |
| sf1608 | stockfish (elo=1608, uci_elo=1608, tiempo=0.1, hash=16) | 1608 |
| aleatorio | aleatorio | — |
| codicioso | codicioso | — |
| minimax1 | minimax (profundidad=1) | — |
| minimax2 | minimax (profundidad=2) | — |

## Elo estimado

Máxima verosimilitud (modelo logístico, tablas = ½) contra las anclas de Elo fijo; IC 95 % por bootstrap (1000 réplicas, remuestreo dentro de cada emparejamiento). Con 0 % o 100 % de puntos no hay máximo finito y no se da cifra.

| Jugador | Partidas | +/=/− | Puntos | Elo | IC 95 % | Derrotas por ilegal | Tablas por tope | Anuladas |
|---|--:|:-:|--:|--:|:-:|--:|--:|--:|
| aleatorio | 480 | 0/15/465 | 7.5 (2%) | 227 | 36 – 340 | 0 | 0 | 0 |
| codicioso | 480 | 70/18/392 | 79 (16%) | 636 | 502 – 720 | 0 | 0 | 0 |
| minimax1 | 480 | 191/86/203 | 234 (49%) | 1233 | 1193 – 1273 | 0 | 0 | 0 |
| minimax2 | 480 | 276/85/119 | 318.5 (66%) | 1405 | 1366 – 1441 | 0 | 0 | 0 |

## Por emparejamiento

| Jugador | Rival | Partidas | Puntos del jugador | Terminaciones |
|---|---|--:|--:|---|
| aleatorio | codicioso | 80 | 5.5 (7%) | checkmate 69, stalemate 7, threefold_repetition 4 |
| aleatorio | minimax1 | 80 | 1 (1%) | checkmate 78, stalemate 1, threefold_repetition 1 |
| aleatorio | minimax2 | 80 | 1 (1%) | checkmate 78, threefold_repetition 2 |
| aleatorio | sf1320 | 80 | 0 (0%) | checkmate 80 |
| aleatorio | sf1468 | 80 | 0 (0%) | checkmate 80 |
| aleatorio | sf1608 | 80 | 0 (0%) | checkmate 80 |
| codicioso | minimax1 | 80 | 2.5 (3%) | checkmate 75, stalemate 1, threefold_repetition 4 |
| codicioso | minimax2 | 80 | 1 (1%) | checkmate 78, threefold_repetition 2 |
| codicioso | sf1320 | 80 | 0 (0%) | checkmate 80 |
| codicioso | sf1468 | 80 | 1 (1%) | checkmate 80 |
| codicioso | sf1608 | 80 | 0 (0%) | checkmate 80 |
| minimax1 | minimax2 | 80 | 20 (25%) | checkmate 40, threefold_repetition 40 |
| minimax1 | sf1320 | 80 | 28.5 (36%) | checkmate 67, stalemate 4, threefold_repetition 9 |
| minimax1 | sf1468 | 80 | 19 (24%) | checkmate 72, threefold_repetition 8 |
| minimax1 | sf1608 | 80 | 10 (12%) | checkmate 62, threefold_repetition 18 |
| minimax2 | sf1320 | 80 | 51.5 (64%) | checkmate 67, stalemate 1, threefold_repetition 12 |
| minimax2 | sf1468 | 80 | 31 (39%) | checkmate 64, threefold_repetition 16 |
| minimax2 | sf1608 | 80 | 18 (22%) | checkmate 68, stalemate 1, threefold_repetition 11 |

## Notas

- El Elo de las anclas de Stockfish es la escala `UCI_Elo` (calibrada en CCRL Blitz a 120 s + 1 s); con un límite fijo por nodos o por tiempo la fuerza real puede diferir. No es comparable con Elo FIDE, Lichess ni Chess.com.
- Las posiciones iniciales vienen de partidas reales de Chess.com; los rivales aparecen solo como «rival (Elo N)».
- Puntuaciones entre el 35 % y el 65 % contra un ancla son las que más informan; si un modelo gana o pierde todo contra un ancla, añade una más fuerte o más débil.

Tandas unidas: `eval/experimento/calibracion`, `eval/experimento/calibracion_b`.
