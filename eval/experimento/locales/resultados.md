# Experimento «locales»

Fecha: 2026-11-08 17:06. Partidas válidas: **60** (anuladas: 0). Gasto en modelos: **0.0000 USD** de un tope de 0.00 USD.

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
| lfm3-1.7b | ollama (modelo=lfm3:1.7b, salida_estructurada=auto, max_tokens=300, reasoning_effort=none) | — |
| qwen3.5-4b | ollama (modelo=qwen3.5:4b, salida_estructurada=auto, max_tokens=300, reasoning_effort=none) | — |
| gemma4-e4b | ollama (modelo=gemma4:e4b-it-qat, salida_estructurada=auto, max_tokens=300, reasoning_effort=none) | — |

## Elo estimado

Máxima verosimilitud (modelo logístico, tablas = ½) contra las anclas de Elo fijo; IC 95 % por bootstrap (1000 réplicas, remuestreo dentro de cada emparejamiento). Con 0 % o 100 % de puntos no hay máximo finito y no se da cifra.

| Jugador | Partidas | +/=/− | Puntos | Elo | IC 95 % | Derrotas por ilegal | Tablas por tope | Anuladas |
|---|--:|:-:|--:|--:|:-:|--:|--:|--:|
| lfm3-1.7b | 20 | 0/0/20 | 0 (0%) | sin estimar (0 %) | — | 20 | 0 | 0 |
| qwen3.5-4b | 20 | 0/0/20 | 0 (0%) | sin estimar (0 %) | — | 20 | 0 | 0 |
| gemma4-e4b | 20 | 0/0/20 | 0 (0%) | sin estimar (0 %) | — | 20 | 0 | 0 |

## Métricas de los LLM

| Jugador | Jugadas | Ilegales / 100 propuestas | Legal a la 1.ª | Tokens entrada / salida por jugada | Coste total | Coste por partida | Latencia p50 / p95 por llamada |
|---|--:|--:|--:|--:|--:|--:|--:|
| lfm3-1.7b | 43 | 80.7 | 26% | 830 / 25 | 0.0000 $ | 0.0000 $ | 0.5 s / 1.3 s |
| qwen3.5-4b | 92 | 68.0 | 36% | 725 / 21 | 0.0000 $ | 0.0000 $ | 0.7 s / 1.2 s |
| gemma4-e4b | 474 | 42.2 | 60% | 507 / 15 | 0.0000 $ | 0.0000 $ | 0.6 s / 0.8 s |

## Por emparejamiento

| Jugador | Rival | Partidas | Puntos del jugador | Terminaciones |
|---|---|--:|--:|---|
| gemma4-e4b | aleatorio | 20 | 0 (0%) | ilegal 20 |
| lfm3-1.7b | aleatorio | 20 | 0 (0%) | ilegal 20 |
| qwen3.5-4b | aleatorio | 20 | 0 (0%) | ilegal 20 |

## Notas

- El Elo de las anclas de Stockfish es la escala `UCI_Elo` (calibrada en CCRL Blitz a 120 s + 1 s); con un límite fijo por nodos o por tiempo la fuerza real puede diferir. No es comparable con Elo FIDE, Lichess ni Chess.com.
- Las posiciones iniciales vienen de partidas reales de Chess.com; los rivales aparecen solo como «rival (Elo N)».
- Puntuaciones entre el 35 % y el 65 % contra un ancla son las que más informan; si un modelo gana o pierde todo contra un ancla, añade una más fuerte o más débil.

## Cómo terminó cada escalera

- lfm3-1.7b: 20 partidas
- qwen3.5-4b: 20 partidas
- gemma4-e4b: 20 partidas
