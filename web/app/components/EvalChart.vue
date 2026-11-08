<script setup lang="ts">
/** Curva de evaluación (desde las blancas) con los errores del usuario marcados en rosa. */
const props = defineProps<{ curve: number[]; errorPlies: number[]; userColor: 'white' | 'black' }>()

const W = 600
const H = 140
const y = (cp: number) => H / 2 - (Math.tanh(cp / 400) * (H / 2 - 8))
const x = (i: number) => (props.curve.length <= 1 ? 0 : (i / (props.curve.length - 1)) * W)

const line = computed(() => props.curve.map((cp, i) => `${x(i).toFixed(1)},${y(cp).toFixed(1)}`).join(' '))
const area = computed(() => `0,${H / 2} ${line.value} ${W},${H / 2}`)
</script>

<template>
  <svg :viewBox="`0 0 ${W} ${H}`" class="chart" role="img" aria-label="Evolución de la evaluación">
    <rect x="0" y="0" :width="W" :height="H / 2" fill="rgba(255,255,255,.04)" />
    <line x1="0" :y1="H / 2" :x2="W" :y2="H / 2" stroke="rgba(255,255,255,.18)" stroke-dasharray="4 4" />
    <polygon :points="area" fill="rgba(255,203,71,.12)" />
    <polyline :points="line" fill="none" stroke="#ffcb47" stroke-width="2.5" vector-effect="non-scaling-stroke" />
    <circle v-for="p in errorPlies" :key="p" :cx="x(p)" :cy="y(curve[p] ?? 0)" r="5" fill="#ff6b95" />
    <text x="6" y="14" class="lbl">blancas</text>
    <text x="6" :y="H - 6" class="lbl">negras</text>
  </svg>
</template>

<style scoped>
.chart {
  width: 100%;
  height: auto;
  display: block;
  border-radius: 10px;
  background: rgba(11, 16, 32, 0.5);
}
.lbl {
  font-size: 11px;
  fill: var(--text-faint);
  font-family: 'Figtree', sans-serif;
}
</style>
