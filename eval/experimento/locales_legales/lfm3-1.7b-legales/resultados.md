# Experimento «locales_legales · lfm3-1.7b-legales»

Fecha: 2026-11-08 17:09. Partidas válidas: **20** (anuladas: 0). Gasto en modelos: **0.0000 USD** de un tope de 0.00 USD.

Reglas: posiciones iniciales tras 4–8 plies de partidas amateur reales (sin repetir, cada una con los dos colores); fin por `board.outcome(claim_draw=True)` o tope de 300 plies (tablas, anotadas aparte); un LLM que agota 3 reintentos pierde por ilegalidad; los errores de API anulan la partida (no cuentan).

## Jugadores

| Jugador | Configuración | Elo fijo |
|---|---|--:|
| aleatorio | aleatorio (elo=227) | 227 |
| lfm3-1.7b-legales | ollama (modelo=lfm3:1.7b, salida_estructurada=auto, max_tokens=300, reasoning_effort=none) | — |

## Elo estimado

Máxima verosimilitud (modelo logístico, tablas = ½) contra las anclas de Elo fijo; IC 95 % por bootstrap (1000 réplicas, remuestreo dentro de cada emparejamiento). Con 0 % o 100 % de puntos no hay máximo finito y no se da cifra.

| Jugador | Partidas | +/=/− | Puntos | Elo | IC 95 % | Derrotas por ilegal | Tablas por tope | Anuladas |
|---|--:|:-:|--:|--:|:-:|--:|--:|--:|
| lfm3-1.7b-legales | 20 | 0/1/19 | 0.5 (2%) | -409 | −∞ – -209 | 19 | 0 | 0 |

## Métricas de los LLM

| Jugador | Jugadas | Ilegales / 100 propuestas | Legal a la 1.ª | Tokens entrada / salida por jugada | Coste total | Coste por partida | Latencia p50 / p95 por llamada |
|---|--:|--:|--:|--:|--:|--:|--:|
| lfm3-1.7b-legales | 112 | 63.4 | 40% | 895 / 20 | 0.0000 $ | 0.0000 $ | 0.4 s / 0.5 s |

## Por emparejamiento

| Jugador | Rival | Partidas | Puntos del jugador | Terminaciones |
|---|---|--:|--:|---|
| lfm3-1.7b-legales | aleatorio | 20 | 0.5 (2%) | ilegal 19, threefold_repetition 1 |

## Notas

- El Elo de las anclas de Stockfish es la escala `UCI_Elo` (calibrada en CCRL Blitz a 120 s + 1 s); con un límite fijo por nodos o por tiempo la fuerza real puede diferir. No es comparable con Elo FIDE, Lichess ni Chess.com.
- Las posiciones iniciales vienen de partidas reales de Chess.com; los rivales aparecen solo como «rival (Elo N)».
- Puntuaciones entre el 35 % y el 65 % contra un ancla son las que más informan; si un modelo gana o pierde todo contra un ancla, añade una más fuerte o más débil.
