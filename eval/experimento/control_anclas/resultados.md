# Experimento «control_anclas»

Fecha: 2026-11-08 17:03. Partidas válidas: **80** (anuladas: 0). Gasto en modelos: **0.0000 USD** de un tope de 0.00 USD.

Reglas: posiciones iniciales tras 4–8 plies de partidas amateur reales (sin repetir, cada una con los dos colores); fin por `board.outcome(claim_draw=True)` o tope de 300 plies (tablas, anotadas aparte); un LLM que agota 3 reintentos pierde por ilegalidad; los errores de API anulan la partida (no cuentan).

## Jugadores

| Jugador | Configuración | Elo fijo |
|---|---|--:|
| sf1468 | stockfish (elo=1468, uci_elo=1468, tiempo=0.1, hash=16) | 1468 |
| sf1608 | stockfish (elo=1608, uci_elo=1608, tiempo=0.1, hash=16) | 1608 |
| sf1320-libre | stockfish (uci_elo=1320, tiempo=0.1, hash=16) | — |

## Elo estimado

Máxima verosimilitud (modelo logístico, tablas = ½) contra las anclas de Elo fijo; IC 95 % por bootstrap (1000 réplicas, remuestreo dentro de cada emparejamiento). Con 0 % o 100 % de puntos no hay máximo finito y no se da cifra.

| Jugador | Partidas | +/=/− | Puntos | Elo | IC 95 % | Derrotas por ilegal | Tablas por tope | Anuladas |
|---|--:|:-:|--:|--:|:-:|--:|--:|--:|
| sf1320-libre | 80 | 14/6/60 | 17 (21%) | 1302 | 1209 – 1385 | 0 | 0 | 0 |

## Por emparejamiento

| Jugador | Rival | Partidas | Puntos del jugador | Terminaciones |
|---|---|--:|--:|---|
| sf1320-libre | sf1468 | 40 | 11 (28%) | checkmate 38, threefold_repetition 2 |
| sf1320-libre | sf1608 | 40 | 6 (15%) | checkmate 36, threefold_repetition 4 |

## Notas

- El Elo de las anclas de Stockfish es la escala `UCI_Elo` (calibrada en CCRL Blitz a 120 s + 1 s); con un límite fijo por nodos o por tiempo la fuerza real puede diferir. No es comparable con Elo FIDE, Lichess ni Chess.com.
- Las posiciones iniciales vienen de partidas reales de Chess.com; los rivales aparecen solo como «rival (Elo N)».
- Puntuaciones entre el 35 % y el 65 % contra un ancla son las que más informan; si un modelo gana o pierde todo contra un ancla, añade una más fuerte o más débil.
