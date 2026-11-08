# Experimento «pago»

Fecha: 2026-11-08 17:06. Partidas válidas: **0** (anuladas: 12). Gasto en modelos: **0.0000 USD** de un tope de 9.50 USD.

Reglas: posiciones iniciales tras 4–8 plies de partidas amateur reales (sin repetir, cada una con los dos colores); fin por `board.outcome(claim_draw=True)` o tope de 300 plies (tablas, anotadas aparte); un LLM que agota 3 reintentos pierde por ilegalidad; los errores de API anulan la partida (no cuentan).

## Jugadores

| Jugador | Configuración | Elo fijo |
|---|---|--:|
| aleatorio | aleatorio (elo=227) | 227 |
| codicioso | codicioso (elo=636) | 636 |
| minimax1 | minimax (profundidad=1, elo=1233) | 1233 |
| sf1320 | stockfish (elo=1320, uci_elo=1320, tiempo=0.1, hash=16) | 1320 |
| minimax2 | minimax (profundidad=2, elo=1405) | 1405 |
| sf1468 | stockfish (elo=1468, uci_elo=1468, tiempo=0.1, hash=16) | 1468 |
| sf1608 | stockfish (elo=1608, uci_elo=1608, tiempo=0.1, hash=16) | 1608 |
| luna-none | azure_openai (deployment=gpt-6-luna, auth=cli, reasoning_effort=none, max_tokens=400, precio_entrada_1m=0.1, precio_salida_1m=0.5) | — |
| luna-high | azure_openai (deployment=gpt-6-luna, auth=cli, reasoning_effort=high, max_tokens=16000, precio_entrada_1m=0.1, precio_salida_1m=0.5) | — |
| haiku-5-5 | claude_foundry (deployment=claude-haiku-5-5, auth=cli, effort=low, max_tokens=4000, precio_entrada_1m=0.1, precio_salida_1m=0.5) | — |
| terra | azure_openai (deployment=gpt-6.1-terra, auth=cli, max_tokens=16000, precio_entrada_1m=1.0, precio_salida_1m=5.0) | — |

## Elo estimado

Máxima verosimilitud (modelo logístico, tablas = ½) contra las anclas de Elo fijo; IC 95 % por bootstrap (1000 réplicas, remuestreo dentro de cada emparejamiento). Con 0 % o 100 % de puntos no hay máximo finito y no se da cifra.

| Jugador | Partidas | +/=/− | Puntos | Elo | IC 95 % | Derrotas por ilegal | Tablas por tope | Anuladas |
|---|--:|:-:|--:|--:|:-:|--:|--:|--:|
| luna-none | 0 | 0/0/0 | 0 (—) | — | — | 0 | 0 | 3 |
| luna-high | 0 | 0/0/0 | 0 (—) | — | — | 0 | 0 | 3 |
| haiku-5-5 | 0 | 0/0/0 | 0 (—) | — | — | 0 | 0 | 3 |
| terra | 0 | 0/0/0 | 0 (—) | — | — | 0 | 0 | 3 |

## Métricas de los LLM

| Jugador | Jugadas | Ilegales / 100 propuestas | Legal a la 1.ª | Tokens entrada / salida por jugada | Coste total | Coste por partida | Latencia p50 / p95 por llamada |
|---|--:|--:|--:|--:|--:|--:|--:|
| luna-none | 0 | — | — | 0 / 0 | 0.0000 $ | — $ | — |
| luna-high | 0 | — | — | 0 / 0 | 0.0000 $ | — $ | — |
| haiku-5-5 | 0 | — | — | 0 / 0 | 0.0000 $ | — $ | — |
| terra | 0 | — | — | 0 / 0 | 0.0000 $ | — $ | — |

## Por emparejamiento

| Jugador | Rival | Partidas | Puntos del jugador | Terminaciones |
|---|---|--:|--:|---|

## Notas

- El Elo de las anclas de Stockfish es la escala `UCI_Elo` (calibrada en CCRL Blitz a 120 s + 1 s); con un límite fijo por nodos o por tiempo la fuerza real puede diferir. No es comparable con Elo FIDE, Lichess ni Chess.com.
- Las posiciones iniciales vienen de partidas reales de Chess.com; los rivales aparecen solo como «rival (Elo N)».
- Puntuaciones entre el 35 % y el 65 % contra un ancla son las que más informan; si un modelo gana o pierde todo contra un ancla, añade una más fuerte o más débil.

## Cómo terminó cada escalera

- luna-none: detenido: 3 errores de API seguidos
- luna-high: detenido: 3 errores de API seguidos
- haiku-5-5: detenido: 3 errores de API seguidos
- terra: detenido: 3 errores de API seguidos
