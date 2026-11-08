<script setup lang="ts">
import { GLYPHS, type PieceMap } from '~/utils/fen'

const props = defineProps<{
  pieces: PieceMap
  orientation: 'white' | 'black'
  legalMoves: string[]
  lastMove?: string | null
  checkSquare?: string | null
  interactive: boolean
  marks?: string[] // casillas a resaltar (p. ej. error del análisis)
}>()
const emit = defineEmits<{ move: [uci: string] }>()

const FILES = 'abcdefgh'
const selected = ref<string | null>(null)
const promotion = ref<{ from: string; to: string } | null>(null)

const squares = computed(() => {
  const out: { name: string; x: number; y: number; light: boolean }[] = []
  for (let row = 0; row < 8; row++) {
    for (let col = 0; col < 8; col++) {
      const file = props.orientation === 'white' ? col : 7 - col
      const rank = props.orientation === 'white' ? 8 - row : row + 1
      out.push({ name: `${FILES[file]}${rank}`, x: col * 100, y: row * 100, light: (file + rank) % 2 === 1 })
    }
  }
  return out
})

const targets = computed(() =>
  selected.value ? new Set(props.legalMoves.filter((m) => m.startsWith(selected.value!)).map((m) => m.slice(2, 4))) : new Set<string>(),
)
const movable = computed(() => new Set(props.legalMoves.map((m) => m.slice(0, 2))))

watch(
  () => props.legalMoves,
  () => {
    selected.value = null
    promotion.value = null
  },
)

function onSquare(name: string) {
  if (!props.interactive) return
  if (selected.value && targets.value.has(name)) {
    const candidates = props.legalMoves.filter((m) => m.startsWith(selected.value! + name))
    if (candidates.length > 1) {
      promotion.value = { from: selected.value, to: name }
      return
    }
    emit('move', candidates[0]!)
    selected.value = null
    return
  }
  selected.value = movable.value.has(name) && selected.value !== name ? name : null
}

function promote(piece: string) {
  if (!promotion.value) return
  emit('move', `${promotion.value.from}${promotion.value.to}${piece}`)
  promotion.value = null
  selected.value = null
}

const isWhite = (p: string) => p === p.toUpperCase()
const last = computed(() => (props.lastMove ? [props.lastMove.slice(0, 2), props.lastMove.slice(2, 4)] : []))
</script>

<template>
  <div class="board-wrap">
    <svg viewBox="0 0 800 800" class="board" role="img" aria-label="Tablero de ajedrez">
      <g v-for="sq in squares" :key="sq.name" class="sq" :class="{ clickable: interactive }" @click="onSquare(sq.name)">
        <rect :x="sq.x" :y="sq.y" width="100" height="100" :fill="sq.light ? 'var(--sq-l)' : 'var(--sq-d)'" />
        <rect v-if="last.includes(sq.name)" :x="sq.x" :y="sq.y" width="100" height="100" fill="rgba(255,203,71,.38)" />
        <rect v-if="marks?.includes(sq.name)" :x="sq.x + 3" :y="sq.y + 3" width="94" height="94" fill="none" stroke="#ff6b95" stroke-width="6" rx="6" />
        <rect v-if="selected === sq.name" :x="sq.x" :y="sq.y" width="100" height="100" fill="rgba(255,107,149,.45)" />
        <circle v-if="checkSquare === sq.name" :cx="sq.x + 50" :cy="sq.y + 50" r="46" fill="rgba(255,60,90,.55)" />
        <text
          v-if="pieces[sq.name]"
          :x="sq.x + 50"
          :y="sq.y + 54"
          class="piece"
          :class="isWhite(pieces[sq.name]!) ? 'w' : 'b'"
        >{{ GLYPHS[pieces[sq.name]!.toLowerCase()] }}</text>
        <circle
          v-if="targets.has(sq.name)"
          :cx="sq.x + 50"
          :cy="sq.y + 50"
          :r="pieces[sq.name] ? 44 : 15"
          :fill="pieces[sq.name] ? 'none' : 'rgba(11,16,32,.45)'"
          :stroke="pieces[sq.name] ? 'rgba(11,16,32,.55)' : 'none'"
          stroke-width="7"
        />
        <text v-if="sq.x === 0" :x="sq.x + 6" :y="sq.y + 20" class="coord" :class="{ onlight: sq.light }">{{ sq.name[1] }}</text>
        <text v-if="sq.y === 700" :x="sq.x + 92" :y="sq.y + 94" class="coord end" :class="{ onlight: sq.light }">{{ sq.name[0] }}</text>
      </g>
    </svg>
    <div v-if="promotion" class="promo">
      <p class="eyebrow">Corona</p>
      <div>
        <button v-for="p in ['q', 'r', 'b', 'n']" :key="p" class="promo-btn" @click="promote(p)">
          {{ GLYPHS[p] }}
        </button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.board-wrap {
  position: relative;
  width: 100%;
  aspect-ratio: 1;
  --sq-l: #d9dff0;
  --sq-d: #6b7ba6;
}
.board {
  width: 100%;
  height: 100%;
  border-radius: 14px;
  box-shadow: 0 24px 60px rgba(0, 0, 0, 0.45), 0 0 0 1px var(--line);
  user-select: none;
  display: block;
}
.sq.clickable {
  cursor: pointer;
}
.piece {
  font-size: 78px;
  text-anchor: middle;
  dominant-baseline: middle;
  font-family: 'Segoe UI Symbol', 'Noto Sans Symbols 2', 'DejaVu Sans', 'Apple Symbols', serif;
  paint-order: stroke;
  stroke-linejoin: round;
  pointer-events: none;
}
.piece.w {
  fill: #fffaf0;
  stroke: #0b1020;
  stroke-width: 5px;
}
.piece.b {
  fill: #0b1020;
  stroke: #e9ecf4;
  stroke-width: 2.5px;
}
.coord {
  font-size: 17px;
  font-weight: 700;
  fill: #d9dff0;
  pointer-events: none;
  font-family: 'Figtree', sans-serif;
}
.coord.onlight {
  fill: #4b5a85;
}
.coord.end {
  text-anchor: end;
}
.promo {
  position: absolute;
  inset: 0;
  display: grid;
  place-content: center;
  text-align: center;
  background: rgba(11, 16, 32, 0.78);
  border-radius: 14px;
}
.promo-btn {
  font-size: 3rem;
  width: 4.5rem;
  height: 4.5rem;
  margin: 0.25rem;
  border-radius: 12px;
  border: 1px solid var(--line);
  background: var(--panel-2);
  color: var(--accent);
}
</style>
