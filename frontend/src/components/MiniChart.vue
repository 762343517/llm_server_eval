<script setup>
import { computed } from 'vue'

// 极简折线图：每条曲线独立缩放（量纲不同：RPS 与秒级延迟），图例里标出各自峰值
const props = defineProps({
  points: { type: Array, default: () => [] },
  series: { type: Array, default: () => [] },
  height: { type: Number, default: 170 },
})

const W = 620
const PAD_X = 30
const PAD_Y = 18

const size = computed(() => ({ w: W, h: props.height }))

const paths = computed(() => {
  const n = props.points.length
  if (n < 2) return []
  const innerW = W - PAD_X * 2
  const innerH = props.height - PAD_Y * 2
  return props.series.map((item) => {
    const values = props.points.map((p) => Number(p[item.key]) || 0)
    const max = Math.max(...values, 1e-9)
    const step = innerW / Math.max(1, n - 1)
    const d = values
      .map((v, i) => {
        const x = PAD_X + step * i
        const y = props.height - PAD_Y - (v / max) * innerH
        return `${i === 0 ? 'M' : 'L'}${x.toFixed(1)},${y.toFixed(1)}`
      })
      .join(' ')
    const last = values[values.length - 1]
    const lastY = props.height - PAD_Y - (last / max) * innerH
    return { ...item, d, max, last, lastX: PAD_X + step * (n - 1), lastY }
  })
})

const gridLines = computed(() => {
  const inner = props.height - PAD_Y * 2
  return [0, 0.25, 0.5, 0.75, 1].map((ratio) => ({
    y: PAD_Y + inner * ratio,
    value: 1 - ratio,
  }))
})

const spanText = computed(() => {
  const n = props.points.length
  if (n < 2) return ''
  return `0s ~ ${Number(props.points[n - 1].t).toFixed(0)}s`
})
</script>

<template>
  <div class="chart">
    <div v-if="points.length < 2" class="empty">数据点不足，压测开始后会自动绘制曲线</div>
    <svg v-else :viewBox="`0 0 ${size.w} ${size.h}`">
      <line
        v-for="line in gridLines"
        :key="line.y"
        :x1="PAD_X"
        :x2="size.w - PAD_X"
        :y1="line.y"
        :y2="line.y"
        stroke="#eef1f6"
        stroke-width="1"
      />
      <path
        v-for="item in paths"
        :key="item.key"
        :d="item.d"
        fill="none"
        :stroke="item.color"
        stroke-width="2"
        stroke-linejoin="round"
      />
      <circle
        v-for="item in paths"
        :key="`${item.key}-dot`"
        :cx="item.lastX"
        :cy="item.lastY"
        r="3"
        :fill="item.color"
      />
      <text :x="PAD_X" :y="size.h - 4" font-size="11" fill="#94a3b8">
        {{ spanText }}
      </text>
    </svg>
    <div class="legend">
      <span v-for="item in paths" :key="item.label">
        <i :style="{ background: item.color }"></i>
        {{ item.label }} 峰值 {{ item.max.toFixed(item.digits ?? 2) }}，当前
        {{ item.last.toFixed(item.digits ?? 2) }}
      </span>
    </div>
  </div>
</template>
