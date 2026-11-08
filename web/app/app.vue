<script setup lang="ts">
import type { GameState } from '~/composables/useApi'
import { applyUci, parseFen } from '~/utils/fen'

const api = useApi()
const auth = useAuth()
const session = auth.session
const PRIVACY_URL = 'https://danielalonso.dev/privacidad'
const TERMS_URL = 'https://danielalonso.dev/terminos'
const CONTACT = 'hola@danielalonso.dev'

const catalog = ref<any>(null)
const apiError = ref<string | null>(null)
const loginButton = ref<HTMLElement | null>(null)

const mode = ref<'llm' | 'clasico'>('clasico')
const classicId = ref('minimax')
const depth = ref(3)
const skill = ref(5)
const temperature = ref(0.7)
const showLegal = ref(false)
const userColor = ref<'white' | 'black'>('white')

const game = ref<GameState | null>(null)
const pending = ref<string | null>(null) // jugada del usuario mostrada mientras piensa el rival
const thinking = ref(false)
const error = ref<string | null>(null)

const diagnosis = ref<any>(null)
const analysing = ref(false)
const history = ref<any[]>([])
const metrics = ref<any[]>([])

async function refreshSide() {
  try {
    history.value = await api.games()
    metrics.value = (await api.metrics()).resumen
  } catch {
    /* la API ya mostrará el error principal */
  }
}

async function loadAfterLogin() {
  apiError.value = null
  try {
    catalog.value = await api.opponents()
  } catch (e: any) {
    if (session.value) apiError.value = `No se puede conectar con la API (${e.message}).`
    return
  }
  // ?game=<id> recupera una partida (y su diagnóstico) tras recargar la página
  const gid = new URL(window.location.href).searchParams.get('game')
  if (gid && !game.value) {
    try {
      game.value = await api.game(gid)
      userColor.value = game.value.user_color
      if (game.value.has_diagnosis) diagnosis.value = await api.getAnalysis(gid)
    } catch (e: any) {
      error.value = e.message
    }
  }
  refreshSide()
}

onMounted(() => {
  auth.restore() // si había sesión, el watch de abajo carga los datos
})

// Al entrar con Google se carga todo; al salir (o si caduca la sesión) se limpia lo del usuario.
watch(
  () => session.value?.token,
  (token, previous) => {
    if (token && token !== previous) {
      loadAfterLogin()
    } else if (!token) {
      catalog.value = null
      game.value = null
      diagnosis.value = null
      history.value = []
      metrics.value = []
    }
  },
)

// El botón de Google se pinta cada vez que aparece su contenedor (al cargar o tras cerrar sesión).
watch(loginButton, (el) => {
  if (el) auth.renderButton(el)
})

watch(
  () => game.value?.id,
  (id) => {
    if (!id) return
    const url = new URL(window.location.href)
    url.searchParams.set('game', id)
    window.history.replaceState(null, '', url)
  },
)

const pieces = computed(() => {
  if (!game.value) return parseFen('rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1')
  const base = parseFen(game.value.fen)
  return pending.value ? applyUci(base, pending.value) : base
})

const checkSquare = computed(() => {
  if (!game.value?.in_check || pending.value) return null
  const king = game.value.turn === 'white' ? 'K' : 'k'
  return Object.entries(pieces.value).find(([, p]) => p === king)?.[0] ?? null
})

const opponentLabel = computed(() => {
  if (mode.value === 'llm') return `IA de Foundry (${catalog.value?.llm?.modelo ?? ''})`
  return catalog.value?.clasicos?.find((o: any) => o.id === classicId.value)?.nombre ?? classicId.value
})

async function start() {
  error.value = null
  diagnosis.value = null
  pending.value = null
  thinking.value = true
  const opponent =
    mode.value === 'llm'
      ? { type: 'llm', temperature: temperature.value, show_legal_moves: showLegal.value }
      : { type: classicId.value, depth: depth.value, skill: skill.value }
  try {
    game.value = await api.newGame({ opponent, user_color: userColor.value })
  } catch (e: any) {
    error.value = e.message
  } finally {
    thinking.value = false
  }
}

async function onMove(uci: string) {
  if (!game.value || thinking.value) return
  error.value = null
  pending.value = uci
  thinking.value = true
  try {
    game.value = await api.move(game.value.id, uci)
  } catch (e: any) {
    error.value = e.message
  } finally {
    pending.value = null
    thinking.value = false
  }
  if (game.value?.status === 'terminada' || game.value?.opponent?.type === 'llm') refreshSide()
}

async function resign() {
  if (!game.value) return
  game.value = await api.resign(game.value.id)
  refreshSide()
}

async function analyse() {
  if (!game.value) return
  analysing.value = true
  error.value = null
  try {
    diagnosis.value = await api.analyse(game.value.id)
  } catch (e: any) {
    error.value = e.message
  } finally {
    analysing.value = false
    refreshSide()
  }
}

const movePairs = computed(() => {
  const san = game.value?.moves_san ?? []
  const out: { n: number; w?: string; b?: string }[] = []
  for (let i = 0; i < san.length; i += 2) out.push({ n: i / 2 + 1, w: san[i], b: san[i + 1] })
  return out
})

const resultText = computed(() => {
  const g = game.value
  if (!g || g.status !== 'terminada') return ''
  const r = { victoria: 'Has ganado', derrota: 'Has perdido', tablas: 'Tablas' }[g.resultado_usuario ?? 'tablas']
  return `${r}: ${g.motivo_fin} (${g.result})`
})

const llmLog = computed(() => game.value?.last_opponent_meta?.registro ?? null)
const errorPlies = computed(() => (diagnosis.value?.diagnostico?.errores ?? []).map((e: any) => e.ply))
const userTurn = computed(() => !!game.value && game.value.legal_moves.length > 0 && !thinking.value)
</script>

<template>
  <div class="page">
    <header class="hero">
      <p class="eyebrow">Entrenador de ajedrez con Microsoft Foundry</p>
      <h1>Mea <span class="accent">culpa</span></h1>
      <p class="lead muted">
        Juega una partida y te digo, en castellano, <strong>por qué la has perdido</strong>. El motor encuentra los errores; la IA
        solo los explica.
      </p>
      <p v-if="catalog" class="faint">
        LLM: <span class="pill">{{ catalog.llm.proveedor }}, {{ catalog.llm.modelo }}</span>
        &nbsp;Análisis: <span class="pill">{{ catalog.motor_analisis }}</span>
        &nbsp;Stockfish: <span class="pill">{{ catalog.stockfish ? 'disponible' : 'no instalado' }}</span>
      </p>
      <p v-if="session" class="faint who">
        <img v-if="session.picture" :src="session.picture" alt="" class="avatar" referrerpolicy="no-referrer" />
        Hola, {{ session.name ?? 'jugador' }}
        <button v-if="!auth.localMode" class="link-btn" @click="auth.signOut()">Cerrar sesión</button>
      </p>
      <p v-if="apiError" class="alert">{{ apiError }}</p>
    </header>

    <section v-if="!session" class="panel login">
      <p class="eyebrow">Entra para jugar</p>
      <p class="muted">
        Tus partidas y tus patrones de error se guardan a tu nombre para poder decirte «otra vez has perdido por…». Solo
        se usa tu cuenta de Google para identificarte; nadie más ve tus partidas.
      </p>
      <div ref="loginButton" class="gsi-button" />
      <p class="faint small">
        Al continuar aceptas las <a :href="TERMS_URL" target="_blank" rel="noopener">condiciones de uso</a> y la
        <a :href="PRIVACY_URL" target="_blank" rel="noopener">política de privacidad</a>.
      </p>
      <p v-if="auth.authError.value" class="alert">{{ auth.authError.value }}</p>
    </section>

    <main v-else class="grid">
      <section class="board-col">
        <ChessBoard
          :pieces="pieces"
          :orientation="game?.user_color ?? userColor"
          :legal-moves="userTurn ? game!.legal_moves : []"
          :last-move="pending ?? game?.last_move"
          :check-square="checkSquare"
          :interactive="userTurn"
          @move="onMove"
        />
        <div class="status">
          <template v-if="!game"><span class="muted">Elige rival y pulsa «Nueva partida».</span></template>
          <template v-else-if="game.status === 'terminada'"><strong class="accent">{{ resultText }}</strong></template>
          <template v-else-if="thinking"><span class="dot" /> {{ opponentLabel }} está pensando…</template>
          <template v-else>Te toca mover ({{ game.user_color === 'white' ? 'blancas' : 'negras' }})</template>
        </div>
        <p v-if="error" class="alert">{{ error }}</p>

        <div v-if="game" class="panel moves">
          <p class="eyebrow">Jugadas</p>
          <ol class="movelist">
            <li v-for="p in movePairs" :key="p.n">
              <span class="faint">{{ p.n }}.</span> <span>{{ p.w }}</span> <span>{{ p.b }}</span>
            </li>
          </ol>
          <div class="row">
            <button class="btn ghost" :disabled="game.status !== 'en_curso' || thinking" @click="resign">Abandonar</button>
            <button class="btn pink" :disabled="analysing || game.moves_uci.length < 2" @click="analyse">
              {{ analysing ? 'Analizando… (unos segundos)' : 'Mea culpa: ¿por qué he perdido?' }}
            </button>
          </div>
        </div>
      </section>

      <section class="side-col">
        <div class="panel">
          <p class="eyebrow">Rival</p>
          <div class="tabs">
            <button :class="{ on: mode === 'clasico' }" @click="mode = 'clasico'">Algoritmos clásicos</button>
            <button :class="{ on: mode === 'llm' }" @click="mode = 'llm'">IA de Foundry</button>
          </div>

          <template v-if="mode === 'clasico'">
            <div class="opps">
              <label v-for="o in catalog?.clasicos ?? []" :key="o.id" class="opp" :class="{ on: classicId === o.id }">
                <input v-model="classicId" type="radio" name="opp" :value="o.id" />
                <span class="opp-name">{{ o.nombre }} <span class="lvl">{{ '★'.repeat(o.nivel) }}</span></span>
                <span class="faint">{{ o.descripcion }}</span>
              </label>
            </div>
            <p v-if="catalog && !catalog.clasicos.some((o: any) => o.id === 'stockfish')" class="faint">
              Stockfish no aparece porque no está instalado (ni en el PATH ni en STOCKFISH_PATH).
            </p>
            <label v-if="classicId === 'minimax'" class="field">
              Profundidad: <strong class="accent">{{ depth }}</strong> plies
              <input v-model.number="depth" type="range" min="1" max="4" step="1" />
            </label>
            <label v-if="classicId === 'stockfish'" class="field">
              Nivel Stockfish: <strong class="accent">{{ skill }}</strong>
              <input v-model.number="skill" type="range" min="0" max="20" step="1" />
            </label>
          </template>

          <template v-else>
            <p class="muted small">
              Un modelo de chat desplegado en Foundry elige sus jugadas. Cada propuesta se valida con python-chess: si es ilegal,
              se le devuelve el error y reintenta (máx. {{ catalog?.llm?.max_reintentos ?? 3 }}); si sigue fallando, juega una
              jugada legal de respaldo y queda registrado.
            </p>
            <label class="field">
              Temperatura: <strong class="accent">{{ temperature.toFixed(1) }}</strong>
              <input v-model.number="temperature" type="range" min="0" max="1.5" step="0.1" />
            </label>
            <label class="check"><input v-model="showLegal" type="checkbox" /> Darle la lista de jugadas legales</label>
          </template>

          <div class="row">
            <div class="colors">
              <button :class="{ on: userColor === 'white' }" @click="userColor = 'white'">♔ Blancas</button>
              <button :class="{ on: userColor === 'black' }" @click="userColor = 'black'">♚ Negras</button>
            </div>
            <button class="btn" :disabled="thinking || !!apiError" @click="start">Nueva partida</button>
          </div>
        </div>

        <div v-if="llmLog" class="panel">
          <p class="eyebrow">Lo que propuso la IA</p>
          <ul class="attempts">
            <li v-for="(a, i) in llmLog.attempts" :key="i" :class="a.valida ? 'ok' : 'ko'">
              <strong>{{ a.propuesta ?? '(ninguna)' }}</strong>
              <span v-if="a.valida"> ✓ legal</span>
              <span v-else> ✗ {{ a.error }}</span>
            </li>
          </ul>
          <p v-if="llmLog.fallback" class="faint">Agotó los reintentos: se jugó una jugada de respaldo.</p>
        </div>

        <div v-if="diagnosis" class="panel diag">
          <p class="eyebrow">Diagnóstico</p>
          <template v-if="diagnosis.diagnostico">
            <h2 class="titular">{{ diagnosis.diagnostico.titular }}</h2>
            <EvalChart
              :curve="diagnosis.analisis.curva_blancas"
              :error-plies="errorPlies"
              :user-color="game?.user_color ?? 'white'"
            />
            <ul class="errors">
              <li v-for="e in diagnosis.diagnostico.errores" :key="e.ply" :class="{ main: e.ply === diagnosis.diagnostico.error_principal_ply }">
                <div class="err-head">
                  <strong>{{ e.jugada_numero }}. {{ e.jugada }}</strong>
                  <span class="pill">{{ e.patron_texto }}</span>
                  <span class="faint">{{ e.gravedad }}</span>
                </div>
                <p>{{ e.explicacion }}</p>
                <p class="faint">Eval: {{ e.eval_antes }} → {{ e.eval_despues }}, mejor según el motor: <strong>{{ e.mejor_jugada }}</strong></p>
              </li>
            </ul>
            <div v-if="diagnosis.diagnostico.patrones_recurrentes.length" class="recurring">
              <p class="eyebrow">Se repite</p>
              <p v-for="r in diagnosis.diagnostico.patrones_recurrentes" :key="r.patron">
                <span class="accent">{{ r.patron_texto }}</span>: {{ r.descripcion }}
              </p>
            </div>
            <p class="advice"><span class="eyebrow">Consejo</span><br />{{ diagnosis.diagnostico.consejo }}</p>
          </template>
          <p v-else class="alert">El LLM no devolvió un diagnóstico válido: {{ diagnosis.error }}</p>
          <p class="faint">
            Motor: {{ diagnosis.analisis.motor }} ({{ diagnosis.analisis.motor_ms }} ms). LLM: {{ diagnosis.modelo }} ({{ diagnosis.latency_ms }} ms,
            {{ diagnosis.input_tokens }}+{{ diagnosis.output_tokens }} tokens, ${{ diagnosis.coste_usd.toFixed(5) }}). Validación:
            {{ diagnosis.grounding.errores_inventados_descartados ?? 0 }} errores inventados descartados
          </p>
        </div>

        <div v-if="history.length" class="panel">
          <p class="eyebrow">Tus partidas</p>
          <table>
            <thead><tr><th>Rival</th><th>Resultado</th><th>Patrones</th></tr></thead>
            <tbody>
              <tr v-for="h in history.slice(0, 8)" :key="h.id">
                <td>{{ h.rival }}</td>
                <td>{{ h.resultado_usuario ?? 'en curso' }}</td>
                <td class="faint">{{ h.patrones.join(', ') || '(ninguno)' }}</td>
              </tr>
            </tbody>
          </table>
        </div>

        <div v-if="metrics.length" class="panel">
          <p class="eyebrow">Jugadas ilegales por modelo</p>
          <table>
            <thead><tr><th>Modelo</th><th>Temp.</th><th>Jugadas</th><th>Ilegales</th><th>1ª legal</th></tr></thead>
            <tbody>
              <tr v-for="m in metrics" :key="m.modelo + m.temperatura">
                <td>{{ m.modelo }}</td>
                <td>{{ m.temperatura }}</td>
                <td>{{ m.jugadas }}</td>
                <td>{{ (m.tasa_ilegal * 100).toFixed(0) }}%</td>
                <td>{{ (m.legal_a_la_primera * 100).toFixed(0) }}%</td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>
    </main>

    <footer class="faint foot">
      <p>
        Mea culpa, proyecto de <a href="https://danielalonso.dev">Daniel Alonso</a> (python-chess, FastAPI, Nuxt y
        Microsoft Foundry)
      </p>
      <p>
        <a :href="PRIVACY_URL">Privacidad</a>, <a :href="TERMS_URL">Condiciones</a>. Contacto:
        <a :href="`mailto:${CONTACT}`">{{ CONTACT }}</a>
      </p>
    </footer>
  </div>
</template>

<style scoped>
.page {
  max-width: 1180px;
  margin: 0 auto;
  padding: 2.5rem 16px 3rem;
}
.hero h1 {
  font-size: clamp(2.6rem, 6vw, 4.2rem);
  font-weight: 800;
}
.lead {
  max-width: 640px;
  font-size: 1.1rem;
  margin: 0.6rem 0 0.8rem;
}
.grid {
  display: grid;
  grid-template-columns: minmax(0, 1.1fr) minmax(0, 1fr);
  gap: 1.5rem;
  margin-top: 1.5rem;
  align-items: start;
}
@media (max-width: 880px) {
  .grid {
    grid-template-columns: minmax(0, 1fr);
  }
}
.board-col,
.side-col {
  display: grid;
  gap: 1rem;
  min-width: 0;
}
.status {
  font-weight: 600;
  min-height: 1.6rem;
}
.dot {
  display: inline-block;
  width: 9px;
  height: 9px;
  border-radius: 50%;
  background: var(--accent);
  animation: pulse 1s infinite alternate;
  margin-right: 0.3rem;
}
@keyframes pulse {
  from {
    opacity: 0.3;
  }
  to {
    opacity: 1;
  }
}
.alert {
  color: var(--bad);
  font-weight: 600;
}
.movelist {
  list-style: none;
  padding: 0;
  margin: 0 0 1rem;
  display: flex;
  flex-wrap: wrap;
  gap: 0.25rem 1rem;
  font-variant-numeric: tabular-nums;
  max-height: 9rem;
  overflow-y: auto;
}
.movelist li span + span {
  margin-left: 0.3rem;
}
.row {
  display: flex;
  gap: 0.75rem;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  margin-top: 1rem;
}
.tabs,
.colors {
  display: inline-flex;
  background: rgba(11, 16, 32, 0.6);
  border-radius: 999px;
  padding: 4px;
  gap: 4px;
  margin-bottom: 1rem;
}
.colors {
  margin-bottom: 0;
}
.tabs button,
.colors button {
  border: 0;
  background: transparent;
  color: var(--text-soft);
  padding: 0.45rem 0.95rem;
  border-radius: 999px;
  font-weight: 600;
}
.tabs button.on,
.colors button.on {
  background: var(--panel-2);
  color: var(--accent);
}
.opps {
  display: grid;
  gap: 0.5rem;
}
.opp {
  display: grid;
  gap: 0.1rem;
  padding: 0.65rem 0.85rem;
  border: 1px solid var(--line);
  border-radius: 12px;
  cursor: pointer;
}
.opp input {
  display: none;
}
.opp.on {
  border-color: var(--accent);
  background: rgba(255, 203, 71, 0.06);
}
.opp-name {
  font-weight: 700;
}
.lvl {
  color: var(--accent);
  font-size: 0.75rem;
  letter-spacing: 0.1em;
}
.field {
  display: block;
  margin-top: 1rem;
}
.check {
  display: flex;
  gap: 0.5rem;
  align-items: center;
  margin-top: 0.75rem;
}
.small {
  font-size: 0.92rem;
}
.attempts {
  list-style: none;
  margin: 0;
  padding: 0;
  display: grid;
  gap: 0.3rem;
}
.attempts .ok {
  color: var(--good);
}
.attempts .ko {
  color: var(--bad);
}
.titular {
  font-size: 1.6rem;
  color: var(--accent);
  margin-bottom: 1rem;
}
.errors {
  list-style: none;
  padding: 0;
  margin: 1rem 0;
  display: grid;
  gap: 0.75rem;
}
.errors li {
  border-left: 3px solid var(--line);
  padding-left: 0.85rem;
}
.errors li.main {
  border-left-color: var(--eyebrow);
}
.errors p {
  margin: 0.25rem 0;
}
.err-head {
  display: flex;
  gap: 0.6rem;
  align-items: center;
  flex-wrap: wrap;
}
.advice {
  background: rgba(255, 203, 71, 0.07);
  border-radius: 12px;
  padding: 0.75rem 1rem;
}
.advice .eyebrow {
  margin: 0;
}
.foot {
  margin-top: 3rem;
  text-align: center;
}
.foot p {
  margin: 0.25rem 0;
}
.login {
  max-width: 560px;
  margin-top: 1.5rem;
  display: grid;
  gap: 0.75rem;
}
.login p {
  margin: 0;
}
.gsi-button {
  min-height: 44px;
}
.who {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  flex-wrap: wrap;
}
.avatar {
  width: 24px;
  height: 24px;
  border-radius: 50%;
}
.link-btn {
  border: 0;
  background: none;
  color: var(--link);
  padding: 0;
  font: inherit;
  cursor: pointer;
  text-decoration: underline;
}
</style>
