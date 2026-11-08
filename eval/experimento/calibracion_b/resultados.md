# Experimento «calibracion_b»

Fecha: 2026-11-08 16:58. Partidas válidas: **720** (anuladas: 0). Gasto en modelos: **0.0000 USD** de un tope de 0.00 USD.

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
| aleatorio | 240 | 0/7/233 | 3.5 (1%) | 231 | -54 – 378 | 0 | 0 | 0 |
| codicioso | 240 | 36/8/196 | 40 (17%) | 655 | 498 – 748 | 0 | 0 | 0 |
| minimax1 | 240 | 94/50/96 | 119 (50%) | 1245 | 1188 – 1294 | 0 | 0 | 0 |
| minimax2 | 240 | 136/44/60 | 158 (66%) | 1401 | 1352 – 1451 | 0 | 0 | 0 |

## Por emparejamiento

| Jugador | Rival | Partidas | Puntos del jugador | Terminaciones |
|---|---|--:|--:|---|
| aleatorio | codicioso | 40 | 2 (5%) | checkmate 36, stalemate 2, threefold_repetition 2 |
| aleatorio | minimax1 | 40 | 1 (2%) | checkmate 38, stalemate 1, threefold_repetition 1 |
| aleatorio | minimax2 | 40 | 0.5 (1%) | checkmate 39, threefold_repetition 1 |
| aleatorio | sf1320 | 40 | 0 (0%) | checkmate 40 |
| aleatorio | sf1468 | 40 | 0 (0%) | checkmate 40 |
| aleatorio | sf1608 | 40 | 0 (0%) | checkmate 40 |
| codicioso | minimax1 | 40 | 1.5 (4%) | checkmate 37, stalemate 1, threefold_repetition 2 |
| codicioso | minimax2 | 40 | 0.5 (1%) | checkmate 39, threefold_repetition 1 |
| codicioso | sf1320 | 40 | 0 (0%) | checkmate 40 |
| codicioso | sf1468 | 40 | 0 (0%) | checkmate 40 |
| codicioso | sf1608 | 40 | 0 (0%) | checkmate 40 |
| minimax1 | minimax2 | 40 | 10.5 (26%) | checkmate 19, threefold_repetition 21 |
| minimax1 | sf1320 | 40 | 17 (42%) | checkmate 32, stalemate 3, threefold_repetition 5 |
| minimax1 | sf1468 | 40 | 8 (20%) | checkmate 36, threefold_repetition 4 |
| minimax1 | sf1608 | 40 | 6 (15%) | checkmate 28, threefold_repetition 12 |
| minimax2 | sf1320 | 40 | 28.5 (71%) | checkmate 35, threefold_repetition 5 |
| minimax2 | sf1468 | 40 | 12.5 (31%) | checkmate 31, threefold_repetition 9 |
| minimax2 | sf1608 | 40 | 8.5 (21%) | checkmate 33, stalemate 1, threefold_repetition 6 |

## Notas

- El Elo de las anclas de Stockfish es la escala `UCI_Elo` (calibrada en CCRL Blitz a 120 s + 1 s); con un límite fijo por nodos o por tiempo la fuerza real puede diferir. No es comparable con Elo FIDE, Lichess ni Chess.com.
- Las posiciones iniciales vienen de partidas reales de Chess.com; los rivales aparecen solo como «rival (Elo N)».
- Puntuaciones entre el 35 % y el 65 % contra un ancla son las que más informan; si un modelo gana o pierde todo contra un ancla, añade una más fuerte o más débil.
