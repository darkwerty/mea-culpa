# Experimento «calibracion»

Fecha: 2026-11-08 16:46. Partidas válidas: **720** (anuladas: 0). Gasto en modelos: **0.0000 USD** de un tope de 0.00 USD.

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
| aleatorio | 240 | 0/8/232 | 4 (2%) | 220 | -64 – 376 | 0 | 0 | 0 |
| codicioso | 240 | 34/10/196 | 39 (16%) | 616 | 319 – 744 | 0 | 0 | 0 |
| minimax1 | 240 | 97/36/107 | 115 (48%) | 1221 | 1156 – 1274 | 0 | 0 | 0 |
| minimax2 | 240 | 140/41/59 | 160.5 (67%) | 1408 | 1350 – 1461 | 0 | 0 | 0 |

## Por emparejamiento

| Jugador | Rival | Partidas | Puntos del jugador | Terminaciones |
|---|---|--:|--:|---|
| aleatorio | codicioso | 40 | 3.5 (9%) | checkmate 33, stalemate 5, threefold_repetition 2 |
| aleatorio | minimax1 | 40 | 0 (0%) | checkmate 40 |
| aleatorio | minimax2 | 40 | 0.5 (1%) | checkmate 39, threefold_repetition 1 |
| aleatorio | sf1320 | 40 | 0 (0%) | checkmate 40 |
| aleatorio | sf1468 | 40 | 0 (0%) | checkmate 40 |
| aleatorio | sf1608 | 40 | 0 (0%) | checkmate 40 |
| codicioso | minimax1 | 40 | 1 (2%) | checkmate 38, threefold_repetition 2 |
| codicioso | minimax2 | 40 | 0.5 (1%) | checkmate 39, threefold_repetition 1 |
| codicioso | sf1320 | 40 | 0 (0%) | checkmate 40 |
| codicioso | sf1468 | 40 | 1 (2%) | checkmate 40 |
| codicioso | sf1608 | 40 | 0 (0%) | checkmate 40 |
| minimax1 | minimax2 | 40 | 9.5 (24%) | checkmate 21, threefold_repetition 19 |
| minimax1 | sf1320 | 40 | 11.5 (29%) | checkmate 35, stalemate 1, threefold_repetition 4 |
| minimax1 | sf1468 | 40 | 11 (28%) | checkmate 36, threefold_repetition 4 |
| minimax1 | sf1608 | 40 | 4 (10%) | checkmate 34, threefold_repetition 6 |
| minimax2 | sf1320 | 40 | 23 (57%) | checkmate 32, stalemate 1, threefold_repetition 7 |
| minimax2 | sf1468 | 40 | 18.5 (46%) | checkmate 33, threefold_repetition 7 |
| minimax2 | sf1608 | 40 | 9.5 (24%) | checkmate 35, threefold_repetition 5 |

## Notas

- El Elo de las anclas de Stockfish es la escala `UCI_Elo` (calibrada en CCRL Blitz a 120 s + 1 s); con un límite fijo por nodos o por tiempo la fuerza real puede diferir. No es comparable con Elo FIDE, Lichess ni Chess.com.
- Las posiciones iniciales vienen de partidas reales de Chess.com; los rivales aparecen solo como «rival (Elo N)».
- Puntuaciones entre el 35 % y el 65 % contra un ancla son las que más informan; si un modelo gana o pierde todo contra un ancla, añade una más fuerte o más débil.
