# Evaluación de Mea culpa — proveedor `mock` / modelo `mock-llm`

Motor de análisis: alfa-beta propio (profundidad 3). Partidas: 6. Fecha: 2026-11-08 15:16.

> ⚠️ Proveedor **mock**: estas cifras validan el pipeline (motor → hechos → LLM → validación),
> no la calidad de ningún modelo. El mock elige siempre la mayor caída, así que la coincidencia
> es 100 % por construcción. Ejecuta con `LLM_PROVIDER=azure` para medir un modelo real.

## 1. ¿El LLM señala el mismo error que el motor?

| Partida | Mayor caída (motor) | Error principal (LLM) | ¿Coincide? | ¿Patrón OK? | Inventados descartados | Motor | LLM | Coste |
|---|---|---|:-:|:-:|:-:|--:|--:|--:|
| inmortal_1851 | 18. Bxg1 (−350 cp) | Bxg1 | ✅ | ✅ | 0 | 13.6 s | 0 ms | $0.00043 |
| legal_1750 | 5. Bxd1 (−835 cp) | Bxd1 | ✅ | ✅ | 0 | 2.4 s | 0 ms | $0.00044 |
| mate_del_loco | 2. g4 (−930 cp) | g4 | ✅ | ✅ | 0 | 0.2 s | 0 ms | $0.00047 |
| mate_del_pastor | 3. Nf6 (−1025 cp) | Nf6 | ✅ | ✅ | 0 | 1.3 s | 0 ms | $0.00047 |
| opera_1858 | 15. Nxd7 (−600 cp) | Nxd7 | ✅ | ✅ | 0 | 7.1 s | 0 ms | $0.00056 |
| siempreviva_1852 | 7. d3 (−195 cp) | d3 | ✅ | ✅ | 0 | 28.7 s | 0 ms | $0.00057 |

- Coincidencia error principal ↔ mayor caída del motor: **100%**
- Patrón del error principal dentro de las etiquetas del motor: **100%**
- Errores inventados por el LLM (descartados por la validación): **0**
- Latencia LLM (mediana): **0 ms** · coste medio por análisis: **$0.00049** (precios en `LLM_PRICE_*_PER_M`)

## 2. Jugadas ilegales propuestas por el modelo

Posiciones: una de cada 4 plies de las partidas de muestra. Reintentos máximos: 3.

| Modelo | Temperatura | Posiciones | Propuestas | Ilegales | Tasa ilegal | Legal a la 1ª | Fallbacks |
|---|--:|--:|--:|--:|--:|--:|--:|
| mock-llm | 0.0 | 40 | 42 | 2 | 4.8% | 95.0% | 0 |
| mock-llm | 0.7 | 40 | 53 | 13 | 24.5% | 72.5% | 0 |
| mock-llm | 1.2 | 40 | 77 | 37 | 48.1% | 30.0% | 0 |
