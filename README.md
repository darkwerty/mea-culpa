# Mea culpa

> «Otra vez has perdido por no enrocar a tiempo.»

Un entrenador de ajedrez que, al acabar la partida, te dice **en castellano y sin rodeos por qué has perdido**. Lo que dice
no se lo inventa: un motor de ajedrez encuentra los errores y un LLM desplegado en **Microsoft Foundry** solo los
explica.

![Diagnóstico tras una partida contra el minimax](docs/diagnostico.png)

## El problema

Los programas de ajedrez te enseñan una evaluación (`-3.2`) y una línea de 12 jugadas. Eso no le sirve a quien está
aprendiendo. Un LLM sí sabe explicar, pero juega fatal y analiza peor: se inventa variantes, piezas y jugadas que no
existen. Mea culpa separa las dos cosas:

- **El motor decide qué pasó** (evaluaciones, mejor jugada, qué pieza quedó colgada).
- **El LLM solo lo cuenta**, en una frase por error, y agrupa los patrones que se repiten en tus partidas.
- **Todo lo que dice el LLM se valida contra los hechos del motor**; lo que no esté respaldado se descarta y se cuenta.

## Resultados (lo que hay medido hoy)

Todo lo de abajo se ha medido **en modo mock** (sin Azure), así que valida el *pipeline*, no la calidad de un modelo.
El script de evaluación está listo para repetirse contra un modelo real de Foundry (ver [Evaluación](#evaluación)).

| Qué | Resultado |
|---|---|
| Tests | **135 tests en verde** (`pytest`; 134 + 1 que solo corre con Stockfish instalado): validación de jugadas, reintentos/fallback, modo experimento (derrota por ilegalidad), tope de gasto, extracción de jugadas en texto, proveedores Azure OpenAI, Claude en Foundry y Ollama con red simulada, Stockfish como ancla, aperturas, Elo por máxima verosimilitud y torneo de punta a punta, mates en 1 y 2, detección de errores, validación de la salida del LLM, API, cliente de Azure con red simulada, login con Google (tokens firmados de prueba), aislamiento entre usuarios, cuotas y almacén Blob simulado |
| Detección de errores en partidas históricas | En las miniaturas, la mayor caída del motor es el error clásico: **5…Bxd1??** en el mate de Légal, **2.g4??** en el mate del loco, **3…Nf6??** en el del pastor. En las partidas largas, el alfa-beta a profundidad 3 no siempre coincide con el análisis histórico (en la Siempreviva marca 7…d3 y no 19…Qxf3); con Stockfish sería más fiable |
| Jugadas ilegales del LLM (mock) | 4,8 % a temperatura 0 · 24,5 % a 0,7 · 48,1 % a 1,2 — sin una sola jugada ilegal en el tablero gracias a la validación con python-chess |
| Coste por diagnóstico | ~750 tokens de entrada + ~200 de salida ≈ **0,0005 $** con precios tipo gpt-4.1-mini (estimado; configurable) |
| Análisis con el motor propio | 0,2 s – 30 s por partida según su longitud (Python puro, profundidad 3) |

La tabla completa está en [`eval/resultados_mock.md`](eval/resultados_mock.md).

## Cómo funciona

```mermaid
flowchart LR
    U[Tú en el navegador<br/>Nuxt 4] -->|jugada UCI| API[FastAPI]
    API -->|valida| PC[python-chess]
    API --> R{Rival}
    R -->|clásico| E[Aleatorio · Codicioso<br/>Minimax alfa-beta · Stockfish*]
    R -->|IA| L[LLM en Foundry]
    L -->|propuesta SAN| V{¿Legal?}
    V -->|no: error de vuelta,<br/>máx. N reintentos| L
    V -->|sí| API
    V -->|agotado| F[Jugada de respaldo<br/>registrada]
    API -->|fin de partida| A[Análisis: motor evalúa<br/>cada posición]
    A -->|hechos: jugada, eval antes/después,<br/>mejor jugada, etiquetas| D[LLM explica<br/>salida estructurada pydantic]
    D --> G[Validación contra los hechos<br/>descarta lo inventado]
    G --> U
    API -.->|métricas: ilegales por<br/>modelo y temperatura| M[(JSONL)]
```

\* Stockfish solo aparece si ya está instalado (ver abajo).

### Dos formas de jugar

1. **Tú contra una IA de Foundry.** Un modelo de chat elige sus jugadas. Cada propuesta pasa por python-chess: si es
   ilegal o no se entiende, se le devuelve el error («'Ke9' es ilegal en esta posición») y vuelve a intentarlo, hasta
   `LLM_MAX_RETRIES` veces (3 por defecto). Si sigue fallando, se juega una jugada legal de respaldo y queda apuntado.
   Cada intento se registra para medir **cuántas jugadas ilegales propone cada modelo a cada temperatura**; la página
   lo muestra en una tabla. Opcionalmente se le puede dar la lista de jugadas legales para comparar.
2. **Tú contra algoritmos clásicos**, de menos a más fuerte:
   - *Aleatorio*: cualquier jugada legal.
   - *Codicioso*: captura lo más valioso que pueda (y da mate en una si lo ve).
   - *Minimax alfa-beta*: negamax con poda alfa-beta, profundización iterativa, quiescencia sobre capturas, tabla de
     transposiciones, ordenación MVV-LVA + killer moves y evaluación de material + tablas pieza-casilla. Profundidad
     configurable (1–4).
   - *Stockfish* con nivel 0–20, **solo si hay un binario `stockfish` en el PATH o en `STOCKFISH_PATH`**. El proyecto no
     descarga binarios; si no lo encuentra, la opción no aparece (en este equipo no estaba instalado, así que esa parte
     no se ha probado).

![Rival LLM proponiendo una jugada ilegal y corrigiéndose](docs/rival-llm.png)

### El análisis «mea culpa»

1. Se evalúa cada posición de la partida: con Stockfish si está, si no con el alfa-beta propio a profundidad fija.
   La posición *antes* de tu jugada se busca un ply más que la de *después*, así el horizonte es el mismo y jugar la
   mejor jugada siempre da caída 0.
2. Para cada jugada tuya: `caída = eval(antes) − eval(después)` desde tu punto de vista (recortada a ±10 peones).
   ≥ 3 peones es *grave*, ≥ 1,5 *error*, ≥ 0,7 *imprecisión*.
3. Cada error se etiqueta con reglas deterministas: `mate_permitido`, `mate_no_visto`, `pieza_colgada` (la mejor
   respuesta del rival captura una pieza tuya indefensa o atacada por otra de menos valor), `cambio_desfavorable`,
   `oportunidad_perdida`, `dama_prematura`, `rey_en_el_centro`, `enroque_tardio`, `error_posicional`.
4. El LLM recibe solo esos hechos (más los recuentos de patrones de tus partidas anteriores) y devuelve, con salida
   estructurada pydantic, una frase por error, el error principal, los patrones recurrentes y un consejo.
5. La salida se valida: errores en jugadas que no están en los hechos, patrones que el motor no detectó o patrones
   «recurrentes» sin respaldo en el historial **se descartan y se cuentan** (se ven en la página y en la evaluación).

El PGN y cualquier texto del usuario son **datos no fiables**: van entre etiquetas (`<partida_pgn>`, `<hechos_motor>`),
se les quitan `<` y `>` para que no puedan cerrar las etiquetas, y el PGN se **reconstruye** solo con la línea
principal: sin comentarios, sin variantes y sin cabeceras de texto libre (`Event`, `White`, `Annotator`…), que son
otra vía de inyección; solo se conservan `FEN`/`SetUp` (validados por python-chess) y `Result` (lista cerrada). El
prompt de sistema dice además que cualquier instrucción dentro de los datos se ignora, y la interfaz pinta la salida
del LLM siempre como texto (nunca `v-html`).

## Seguridad y límites de la demo pública

- **Login con Google** (Google Identity Services, botón «Continuar con Google», flujo de ID token, sin client
  secret). El frontend manda el ID token en `Authorization: Bearer`; la API (`backend/meaculpa/auth.py`) lo verifica
  con `google.oauth2.id_token.verify_oauth2_token` (firma con las claves públicas de Google, cacheadas una hora,
  `aud` = `GOOGLE_CLIENT_ID`, `iss`, `exp`) y comprueba además `email_verified == true`. Todas las rutas (bajo
  `/api/meaculpa`) salvo `health` y `models` lo exigen. **Sin `GOOGLE_CLIENT_ID` la API no arranca** y no hay ningún modo sin login activable por
  variable de entorno.
- **Cada usuario solo ve lo suyo.** Las partidas se guardan en `games/{sha256(sub)}/{id}.json`: ni el correo ni el
  `sub` en claro llegan al almacenamiento ni a los logs. Una partida de otro usuario devuelve 404, y el «otra vez has
  perdido por…» solo cuenta tus partidas.
- **Cuotas diarias por usuario** (persistidas en Blob, válidas para todas las réplicas): 30 partidas, 1500 jugadas,
  200 jugadas contra la IA y 20 análisis. **Fusible global** de 3000 llamadas al LLM al día (cada jugada del LLM
  reserva el peor caso, 1 + `LLM_MAX_RETRIES`). Todo configurable (`QUOTA_*`, `GLOBAL_LLM_CALLS_PER_DAY`).
- **El análisis no puede tumbar la API**: como mucho 200 plies (`MAX_ANALYSIS_PLIES`) y 20 000 caracteres de PGN,
  un análisis a la vez por réplica (los demás reciben 503 en 5 s) y 90 s de tiempo máximo (`ANALYSIS_TIMEOUT_S`).
- **Cabeceras**: la API no publica `/docs` ni `/openapi.json` y responde con `nosniff`, `frame-ancestors 'none'` y
  `no-store`. CORS solo admite orígenes exactos (`CORS_ORIGINS` rechaza `*`). El frontend se publica con un
  `staticwebapp.config.json` generado tras `nuxt generate` (`web/scripts/swa-config.mjs`): CSP con los scripts en
  línea de Nuxt permitidos por hash, Google Identity Services y `connect-src` limitado a la URL de la API,
  `frame-ancestors 'none'`, `nosniff`, `Referrer-Policy` y HSTS.
- Junto al botón de login y en el pie: [privacidad](https://danielalonso.dev/privacidad),
  [condiciones](https://danielalonso.dev/terminos) y contacto en hola@danielalonso.dev.

## Ejecutarlo en local (modo mock, sin Azure)

Requisitos: Python 3.11+ y Node 20+.

```bash
# API (servidor de desarrollo: un único usuario local, sin cuenta de Google)
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements-dev.txt -c constraints.txt   # en Linux/macOS: .venv/bin/python
.venv/Scripts/python -m uvicorn dev_server:app --app-dir backend --port 8000

# Frontend (otra terminal). Sin NUXT_PUBLIC_GOOGLE_CLIENT_ID entra en «modo local» sin botón de login.
cd web
npm install
npm run dev            # http://localhost:3000
```

`backend/dev_server.py` acepta cualquier petición como un usuario local. Está fuera del paquete `meaculpa` y el
`Dockerfile` no lo copia, así que no existe en la imagen desplegada. Para probar el login real en local, crea un
cliente OAuth web con `http://localhost:3000` como origen autorizado y arranca la API normal
(`uvicorn meaculpa.main:create_app --factory --app-dir backend`) con `GOOGLE_CLIENT_ID` y el frontend con
`NUXT_PUBLIC_GOOGLE_CLIENT_ID`.

Por defecto `LLM_PROVIDER=mock`: un proveedor simulado que lee los mismos prompts que vería el modelo real (FEN, hechos
del motor) y responde con el mismo esquema. Para jugar propone a veces jugadas ilegales (más cuanta más temperatura),
y para el diagnóstico rellena plantillas en español con los hechos. Sirve para probarlo todo de punta a punta, no para
juzgar la calidad de las explicaciones.

En local las partidas y cuotas se guardan en `data/runtime/store/` y las métricas en `data/runtime/` (ignorado por
git). En Azure, partidas y cuotas van a Blob Storage (`AZURE_STORAGE_ACCOUNT_URL`, con la identidad administrada).

```bash
.venv/Scripts/python -m pytest -q        # tests
.venv/Scripts/python -m ruff check .     # lint
```

## Apuntarlo a Microsoft Foundry

1. Despliega un modelo de chat en un recurso de Foundry / Azure OpenAI (o usa `infra/main.bicep`).
2. Date el rol **Cognitive Services OpenAI User** sobre el recurso y haz `az login`.
3. Variables de entorno:

```bash
LLM_PROVIDER=azure
AZURE_OPENAI_ENDPOINT=https://<tu-recurso>.openai.azure.com/
AZURE_OPENAI_DEPLOYMENT=<nombre-del-deployment>
# Opcionales
AZURE_AUTH=default               # default (DefaultAzureCredential) | cli (AzureCliCredential) | key
AZURE_OPENAI_API_VERSION=        # vacío = API v1 (/openai/v1/), recomendado; o p. ej. 2024-10-21 para el cliente clásico
AZURE_OPENAI_API_KEY=            # solo si no puedes usar Entra ID
AZURE_OPENAI_REASONING_EFFORT=   # none | low | medium | high (vacío = no se envía)
LLM_MAX_TOKENS=                  # vacío = no se envía (va como max_completion_tokens)
AZURE_OPENAI_SUPPORTS_TEMPERATURE=true   # false para modelos de razonamiento que no aceptan temperature
LLM_PRICE_INPUT_PER_M=0.40       # USD por millón de tokens, para calcular el coste
LLM_PRICE_OUTPUT_PER_M=1.60
```

La temperatura **no se envía por defecto** (se usa la del proveedor); solo viaja si se elige explícitamente
(el deslizador de la demo, `--temps` en `evaluate.py` o `temperatura` en un experimento).

Otros proveedores (`LLM_PROVIDER`):

| Proveedor | Variables |
|---|---|
| `claude` (Claude en Foundry, Messages API de Anthropic con el SDK `anthropic.AnthropicFoundry`) | `ANTHROPIC_FOUNDRY_RESOURCE` (→ `https://<recurso>.services.ai.azure.com/anthropic`) o `ANTHROPIC_FOUNDRY_BASE_URL`; `ANTHROPIC_FOUNDRY_DEPLOYMENT` (nombre del deployment, va en `model`); Entra ID con `AZURE_AUTH=cli\|default` (ámbito `https://ai.azure.com/.default`, rol *Cognitive Services User*) o `ANTHROPIC_FOUNDRY_API_KEY`; opcional `ANTHROPIC_FOUNDRY_EFFORT` (low…max) y `LLM_MAX_TOKENS` (4096 por defecto). Nunca envía `temperature`, `top_p` ni `top_k` (Haiku/Sonnet 5.5 devuelven 400). |
| `ollama` (local, API compatible con OpenAI) | `OLLAMA_BASE_URL` (por defecto `http://localhost:11434/v1`), `OLLAMA_MODEL`. Sin coste. |

La jugada se pide en salida estructurada (`{"move": "<SAN>"}`) si el modelo la admite; si no (o si el
servidor rechaza `response_format`), se pide en texto terminado en «Jugada: <SAN>», se extrae con una
expresión regular y se valida igual con python-chess.

El proveedor (`backend/meaculpa/llm/azure.py`) usa el SDK oficial `openai`: con la API v1, el cliente `OpenAI` con
`base_url={endpoint}/openai/v1/`; con `api-version`, el cliente `AzureOpenAI`. La autenticación por defecto es
**Entra ID** (`DefaultAzureCredential` + `get_bearer_token_provider`, ámbito `https://cognitiveservices.azure.com/.default`):
en local usa tu `az login` y en Azure la identidad administrada (`AZURE_CLIENT_ID`). La API key es solo un plan B.
Las llamadas usan `chat.completions.parse(response_format=<modelo pydantic>)`.

Los tests comprueban este cliente sin red: un transporte HTTP falso verifica la URL, la cabecera `Authorization: Bearer`,
el `response_format` JSON Schema y el parseo. **No se ha probado contra un recurso real de Azure.**

Selector de modelo del rival IA: `GET /api/meaculpa/models` devuelve el catálogo de `backend/meaculpa/models.py`
(GPT-4.1 mini, GPT-6 Luna sin razonamiento y con razonamiento alto, GPT-6.1 Terra y Claude Haiku 5.5) con
`disponible` por modelo, y `POST /api/meaculpa/games` acepta `opponent.model`. Solo se activan los ids de
`LLM_MODELS` (separados por comas; `LLM_DEFAULT_MODEL` el que se usa si no se elige); Claude necesita además
`ANTHROPIC_FOUNDRY_RESOURCE`.

Otras variables: `GOOGLE_CLIENT_ID` (obligatoria), `CORS_ORIGINS`, `AZURE_STORAGE_ACCOUNT_URL` +
`AZURE_STORAGE_CONTAINER`, `STOCKFISH_PATH`, `ANALYSIS_DEPTH` (3), `STOCKFISH_ANALYSIS_DEPTH` (14),
`LLM_MAX_RETRIES` (3), `MEA_CULPA_DATA_DIR`, `QUOTA_GAMES_PER_DAY` (30), `QUOTA_MOVES_PER_DAY` (1500),
`QUOTA_LLM_MOVES_PER_DAY` (200), `QUOTA_ANALYSES_PER_DAY` (20), `GLOBAL_LLM_CALLS_PER_DAY` (3000),
`MAX_ANALYSIS_PLIES` (200), `MAX_CONCURRENT_ANALYSES` (1), `ANALYSIS_TIMEOUT_S` (90),
`APPLICATIONINSIGHTS_CONNECTION_STRING` (+ paquete `azure-monitor-opentelemetry`).

## Evaluación

```bash
.venv/Scripts/python backend/scripts/evaluate.py                       # mock
LLM_PROVIDER=azure AZURE_OPENAI_ENDPOINT=... AZURE_OPENAI_DEPLOYMENT=... \
  .venv/Scripts/python backend/scripts/evaluate.py --temps 0 0.7 1.2   # modelo real
```

Sobre las partidas de `data/pgn/` (la Inmortal 1851, la Siempreviva 1852, la de la Ópera 1858, el mate de Légal y dos
miniaturas de ejemplo; todas de dominio público) mide:

- con qué frecuencia el **error principal que nombra el LLM coincide con la mayor caída del motor**;
- si el patrón que elige está entre las etiquetas del motor;
- cuántos errores inventa (descartados por la validación);
- latencia y coste por análisis;
- **tasa de jugadas ilegales por modelo y temperatura**, jugada legal a la primera y fallbacks.

Escribe la tabla en `eval/resultados_<proveedor>.md`. En mock la coincidencia es 100 % por construcción (el mock elige
siempre la mayor caída); el número interesante saldrá al ejecutarlo contra uno o varios deployments de Foundry.

### Experimento de Elo (`backend/scripts/experiment.py`)

Torneo configurable (YAML o JSON; ejemplo completo en [`eval/experimentos/ejemplo.yaml`](eval/experimentos/ejemplo.yaml))
de participantes —motores, deployments de Azure OpenAI/Foundry, Claude en Foundry, modelos de Ollama— contra
**anclas de Elo fijo** (Stockfish con `UCI_LimitStrength` + `UCI_Elo` 1320/1468/1608…, un proceso por partida,
`Threads=1`, hash pequeño, límite constante por nodos o por tiempo).

```bash
export STOCKFISH_PATH="C:/.../stockfish-windows-x86-64-universal.exe"   # o 'stockfish' en el PATH
.venv/Scripts/python backend/scripts/experiment.py eval/experimentos/ejemplo.yaml --dry-run   # sin llamadas
.venv/Scripts/python backend/scripts/experiment.py eval/experimentos/ejemplo.yaml             # juega
```

- **Modo experimento del LLM**: agotar los reintentos es **derrota por ilegalidad** (no juega el codicioso, que
  sigue siendo el respaldo solo en la demo). Por jugada se guardan intentos, ilegales, tokens, latencia y coste.
- **Posiciones no famosas**: cada partida arranca tras 4–8 plies de una partida amateur real (las de Chess.com de
  Daniel), sin repetir y con los dos colores por posición. Los rivales salen solo como «rival (Elo N)».
- **Fin de partida**: `board.outcome(claim_draw=True)` o tope de 300 plies (tablas, contadas aparte). Un error de
  API anula la partida (`*`), nunca son tablas en silencio; tres seguidos detienen el experimento.
- **Tope de gasto duro** (`presupuesto_usd`): cada llamada reserva su peor caso (entrada estimada + `max_tokens`)
  antes de hacerse; si no cabe, el experimento se para. Relanzar con la misma salida continúa donde se quedó.
- **Salida**: `pgn/<id>.pgn` y `partidas.pgn`, `partidas.jsonl` (una fila por partida), `jugadas_llm.jsonl` (una por
  jugada del LLM), `aperturas.json` y `resultados.md` con el **Elo por máxima verosimilitud contra las anclas e
  IC 95 % por bootstrap** (no se da cifra con 0 % o 100 % de puntos), derrotas por ilegal, tablas por tope,
  ilegales por 100 propuestas, legal a la primera, tokens y coste por jugada y latencia p50/p95.

## Infraestructura y despliegue

`infra/main.bicep` + `infra/main.bicepparam` (compilan sin avisos con `az bicep build` y `az bicep lint`), todo en
**francecentral** (la política de la suscripción no admite otras regiones) salvo la Static Web App, fija en
`centralus`:

- **Azure Container Apps** (plan Consumption) para la API, `minReplicas: 0` → escala a cero; `maxReplicas: 1` como
  tope de coste (el estado está en Blob, así que se puede subir con `apiMaxReplicas`).
- **Recurso de Microsoft Foundry existente** (`foundryAccountName`, hoy `meaculpa-fc-7952`, creado para el experimento,
  con `disableLocalAuth`): el Bicep no lo crea, solo le añade el **deployment del modelo** (`gpt-4.1-mini`,
  GlobalStandard, 10K TPM; parametrizable). `gpt-6-luna` no tiene cuota en la suscripción (0 TPM).
- **Azure Container Registry Basic** (sin usuario admin) para la imagen de la API: el repo es privado, así que GHCR
  obligaría a dar credenciales a Container Apps. La app tira de la imagen con su identidad administrada (*AcrPull*).
- **Storage account** (Standard_LRS, sin claves compartidas ni acceso público) con un contenedor para partidas y
  cuotas; los contadores diarios se borran solos a los 30 días.
- **Identidad administrada** asignada por el usuario con *Cognitive Services OpenAI User* sobre Foundry y *Storage
  Blob Data Contributor* sobre la cuenta de almacenamiento.
- **Log Analytics + Application Insights** (con tope diario de ingesta de 1 GB).
- **Static Web Apps (Free)** para el frontend y, en un segundo despliegue, el dominio `meaculpa.danielalonso.dev`.
- **Desde noviembre de 2026 la demo vive en https://danielalonso.dev/meaculpa** (repo de la web personal): la
  Container App está enlazada como backend de la SWA `swa-danielalonso` (plan Standard, ~9 $/mes, requisito para
  enlazar backends), que reenvía `danielalonso.dev/api/*` a la API; por eso todas las rutas cuelgan de
  `/api/meaculpa`. La SWA de este repo solo redirige `meaculpa.danielalonso.dev` a la nueva URL.
- **Azure SQL Database con la oferta gratuita** (`meaculpa-sql-fc` / `meaculpa`, francecentral): usuarios, eventos
  de uso con coste y tokens por modelo, mensajes del formulario de contacto y controles del panel
  `danielalonso.dev/admin`. Serverless GP Gen5 2 vCores con `useFreeLimit` y `freeLimitExhaustionBehavior=AutoPause`:
  si se agota el límite gratuito del mes (100 000 s de vCore y 32 GB) **se pausa hasta el mes siguiente en vez de
  cobrar**, y las demos siguen funcionando sin ella (las escrituras de uso se descartan, los controles usan lo último
  conocido). Solo autenticación de Microsoft Entra (sin usuarios ni contraseñas SQL); la API entra con su identidad
  administrada (`meaculpa-api-id`, solo SELECT/INSERT/UPDATE/DELETE en sus tablas, ver `infra/sql/identidad_api.sql`).
  Firewall: solo «permitir servicios de Azure». Migraciones versionadas en `backend/meaculpa/migrations/`.
- **Presupuesto mensual** (desde 2026-11-01) con alertas al 50 % y 90 % (real) y al 100 % (previsto) a
  admin@example.com.

La imagen (`Dockerfile`, dependencias fijadas con `constraints.txt`) se construye en la nube con `az acr build` y se
guarda en el ACR del grupo. `.github/workflows/ci.yml` (permisos de solo lectura, acciones fijadas por SHA)
pasa ruff y pytest, construye la imagen y comprueba que no arranca sin `GOOGLE_CLIENT_ID`, y ejecuta `nuxt generate`.
`.github/workflows/deploy.yml` despliega en cada push a `master`: `az acr build` → `az deployment group create` (login
OIDC, sin secretos) → `nuxt generate` con la URL de la API → Static Web App.

### Primer despliegue (pasos manuales, una vez)

1. Registrar los proveedores (desde una sesión con permisos sobre la suscripción):
   `az provider register -n <ns> --wait` para `Microsoft.CognitiveServices`, `Microsoft.App`,
   `Microsoft.OperationalInsights`, `Microsoft.Insights`, `Microsoft.ManagedIdentity`, `Microsoft.Storage` y
   `Microsoft.Web`.
2. `az group create -n rg-mea-culpa -l francecentral`.
3. App registration para GitHub con credencial federada `repo:darkwerty/mea-culpa:ref:refs/heads/master`, con rol
   *Contributor* + *Role Based Access Control Administrator* (o *Owner*) sobre el grupo: el Bicep crea asignaciones
   de rol.
4. Cliente OAuth web de Google con orígenes autorizados `https://meaculpa.danielalonso.dev` (y el
   `*.azurestaticapps.net` de la SWA para probar antes del dominio).
5. Variables del repo: `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`, `AZURE_RESOURCE_GROUP`,
   `GOOGLE_CLIENT_ID`. Sin `AZURE_RESOURCE_GROUP` el workflow de despliegue no hace nada.
6. Primer despliegue manual del Bicep con la imagen de ejemplo (crea el ACR), `az acr build` y segundo despliegue con
   `API_IMAGE` (ver abajo). A partir de ahí el workflow encuentra el ACR del grupo por sí solo.
7. En Cloudflare, CNAME `meaculpa` → `defaultHostname` de la SWA en modo **DNS only** y
   `az staticwebapp hostname set -n <swa> -g rg-mea-culpa --hostname meaculpa.danielalonso.dev` (o variable
   `BIND_CUSTOM_DOMAIN=true` para que lo haga el Bicep; si el CNAME no existe aún, esa validación falla).

Base de datos (CLI, una vez; el Bicep solo pasa servidor y base de datos a la Container App):

```bash
az provider register -n Microsoft.Sql --wait
az sql server create -g rg-mea-culpa -n meaculpa-sql-fc -l francecentral --enable-ad-only-auth \
  --external-admin-principal-type User --external-admin-name <tu-upn> --external-admin-sid <tu-object-id>
az sql server firewall-rule create -g rg-mea-culpa -s meaculpa-sql-fc -n AllowAzureServices \
  --start-ip-address 0.0.0.0 --end-ip-address 0.0.0.0
az sql db create -g rg-mea-culpa -s meaculpa-sql-fc -n meaculpa -e GeneralPurpose -f Gen5 -c 2 \
  --compute-model Serverless --use-free-limit --free-limit-exhaustion-behavior AutoPause --backup-storage-redundancy Local
# Migraciones y usuario de la identidad (abre antes una regla de firewall temporal para tu IP y bórrala después):
export AZURE_SQL_SERVER=meaculpa-sql-fc.database.windows.net AZURE_SQL_DATABASE=meaculpa
python backend/scripts/sql_admin.py migrar
python backend/scripts/sql_admin.py sql infra/sql/identidad_api.sql
# Gasto de Azure en /admin:
az role assignment create --assignee-object-id <principalId de meaculpa-api-id> --assignee-principal-type ServicePrincipal \
  --role "Cost Management Reader" --scope /subscriptions/<sub>
```

**Cómo comprobar que la base de datos no cobra**: `az sql db show -g rg-mea-culpa -s meaculpa-sql-fc -n meaculpa
--query "{free:useFreeLimit, alAgotarse:freeLimitExhaustionBehavior, sku:currentSku.name}"` debe dar `true`,
`AutoPause` y `GP_S_Gen5`. En el portal, la página de la base de datos muestra el banner de la oferta gratuita y, en
«Información general», el uso del límite gratuito del mes (segundos de vCore y almacenamiento). En Cost Management la
base de datos aparece con coste 0.

Si se borra el grupo, la cuenta de Foundry queda en *soft-delete*: para recrearla con el mismo nombre, antes
`az cognitiveservices account purge -g rg-mea-culpa -n meaculpa-fc-7952 -l francecentral`.

Despliegue manual equivalente:

```bash
export GOOGLE_CLIENT_ID=<id>.apps.googleusercontent.com
az deployment group create -g rg-mea-culpa -f infra/main.bicep -p infra/main.bicepparam   # 1.ª vez: crea el ACR
az acr build -r <acr> -t mea-culpa-api:<sha> .
export API_IMAGE=<acr>.azurecr.io/mea-culpa-api:<sha>
az deployment group create -g rg-mea-culpa -f infra/main.bicep -p infra/main.bicepparam
```

**Coste estimado** para una demo de portfolio (pocas partidas al día), con precios de referencia que conviene revisar
en la calculadora de Azure antes de desplegar:

| Recurso | Estimación mensual |
|---|---|
| Container Apps (escala a cero, dentro de la concesión gratuita mensual) | ~0 € |
| Log Analytics / App Insights (bajo la franja gratuita de 5 GB) | ~0 € |
| Static Web Apps Free | 0 € |
| Azure SQL Database (oferta gratuita, se pausa al agotarla) | 0 € |
| Azure Container Registry Basic (coste fijo) | ~4,6 € |
| Foundry: tokens (≈0,0005 $ por diagnóstico, ≈0,005 $ por partida contra el LLM) | < 1 € con 100 partidas |
| **Total** | **~5–7 €/mes** |

Ojo: el presupuesto **avisa, no corta**. Los topes reales son las cuotas por usuario, el fusible global de llamadas
al LLM, los 10K TPM del deployment y el escalado máximo a 1 réplica. (El Storage account añade céntimos al mes.)

## Decisiones de diseño

- **Por qué validar con python-chess.** Un LLM no tiene estado del tablero: propone jugadas en texto. python-chess es la
  fuente de verdad de las reglas (enroque, al paso, clavadas, ahogado). El servidor nunca aplica una jugada que no haya
  pasado por `board.legal_moves`, venga del usuario, del LLM o de un motor.
- **Por qué devolverle el error al LLM en vez de descartarlo sin más.** Medir cómo se corrige con feedback es más
  informativo que una tasa de fallos bruta, y es lo que se haría en producción. El fallback garantiza que la partida
  sigue y queda registrado para que no maquille la métrica.
- **Por qué el LLM solo explica hechos del motor.** Es el patrón de *grounding* de cualquier sistema RAG serio aplicado al
  ajedrez: el modelo es bueno redactando y malo calculando. Darle hechos cerrados, pedir salida estructurada y validarla
  después convierte las alucinaciones en un número medible (errores inventados descartados).
- **Por qué alfa-beta propio.** Porque Stockfish no siempre está y no se deben descargar binarios, y porque demuestra
  la base: negamax + poda + quiescencia + tabla de transposiciones en ~200 líneas legibles. Es lento (Python puro) y
  débil comparado con Stockfish, pero suficiente para ver piezas colgadas y mates cortos, que es lo que pierde partidas
  a nivel aficionado.
- **Por qué patrones deterministas.** «Has vuelto a dejar el rey en el centro» solo es honesto si se puede contar. Las
  etiquetas salen de reglas sobre el tablero, se guardan por partida y el LLM recibe los recuentos; no puede decir
  «otra vez» si el historial no lo respalda.
- **Proveedor detrás de una interfaz.** `structured(messages, schema, temperature)` es toda la superficie. El mock
  permite tests y demo sin cuenta de Azure; cambiar de modelo es cambiar una variable de entorno.

## Qué falta

- **No se ha probado contra Azure real**: ni el modelo de Foundry, ni el despliegue Bicep, ni Blob Storage, ni el
  login con un cliente OAuth real (los tests usan tokens firmados con una clave de prueba). La imagen Docker sí se
  construye y arranca en local. Las cifras de calidad del LLM están por medir.
- Stockfish 19 ya está instalado (winget) y el ancla del experimento se ha probado con él; el rival de la demo y el analizador con Stockfish siguen sin probar a fondo. Winget no creó el alias `stockfish` en `%LOCALAPPDATA%/Microsoft/WinGet/Links`: hay que definir `STOCKFISH_PATH`.
- Los proveedores Azure OpenAI y Claude en Foundry solo se han probado con red simulada; Ollama, contra un servidor local real.
- El análisis con el motor propio puede tardar ~30 s en partidas largas; se podría paralelizar por posiciones o pasar a Stockfish.
- Las métricas de jugadas ilegales del LLM (`llm_moves.jsonl`) siguen en disco local: en Container Apps se pierden
  al escalar a cero. No contienen datos de usuarios.
- El límite de un análisis simultáneo es por réplica, no global.
- Las etiquetas de patrones son heurísticas sencillas (no detectan clavadas, horquillas, debilidades de peones…).
- El frontend no tiene arrastrar y soltar ni reloj; la promoción se elige con un selector simple.
- La CI y el despliegue (`.github/workflows/`) están escritos pero el repo aún no tiene remoto, así que nunca se han
  ejecutado en GitHub.

## Estructura

```
backend/meaculpa/
  engines/        evaluación (material + PST), alfa-beta, rivales clásicos, Stockfish (rival y ancla de Elo)
  llm/            interfaz, Azure OpenAI/Foundry, Claude en Foundry, Ollama, mock
  experiment.py   torneo contra anclas: calendario, partidas, PGN/JSONL, informe
  elo.py          Elo por máxima verosimilitud + IC por bootstrap
  openings.py     posiciones iniciales de partidas reales
  llm_player.py   rival LLM: validación, reintentos, fallback, métricas
  analysis.py     análisis mea culpa: evaluaciones, caídas, etiquetas, diagnóstico y validación
  prompts.py      prompts y saneado de datos no fiables
  schemas.py      esquemas pydantic de la salida del LLM
  games.py        partidas por usuario
  auth.py         verificación del ID token de Google
  quota.py        cuotas diarias y fusible del LLM
  storage.py      almacén Blob / ficheros con ETag
  main.py         API FastAPI
backend/dev_server.py  servidor local sin login (no entra en la imagen)
backend/tests/    pytest
backend/scripts/evaluate.py, experiment.py
data/pgn/         partidas de muestra
web/              Nuxt 4 + Vue 3 (+ scripts/swa-config.mjs: cabeceras y CSP de la SWA)
infra/            Bicep
eval/             resultados de la evaluación
```

---

Proyecto de [Daniel Alonso](https://danielalonso.dev).
