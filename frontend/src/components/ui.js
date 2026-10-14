// 小型展示组件（用渲染函数写，避免依赖运行时模板编译器）
import { defineComponent, h } from 'vue'
import { statusKind } from '../format'

export const StatusBadge = defineComponent({
  name: 'StatusBadge',
  props: { status: { type: String, default: '' }, text: { type: String, default: '' } },
  setup: (props) => () =>
    h('span', { class: ['badge', statusKind(props.status)] },
      props.text || props.status || '—'),
})

export const ProgressBar = defineComponent({
  name: 'ProgressBar',
  props: {
    pct: { type: Number, default: 0 },
    status: { type: String, default: '' },
    text: { type: String, default: '' },
  },
  setup: (props) => () => {
    const value = Math.max(0, Math.min(100, Number(props.pct) || 0))
    const cls = props.status === 'succeeded' ? 'done' : props.status === 'failed' ? 'bad' : ''
    return h('div', { class: 'progress-line' }, [
      h('div', { class: ['progress', cls] }, [h('i', { style: { width: `${value}%` } })]),
      h('span', { class: 'text' }, props.text || `${value.toFixed(1)}%`),
    ])
  },
})

export const StatTile = defineComponent({
  name: 'StatTile',
  props: {
    label: { type: String, default: '' },
    value: { type: [String, Number], default: '—' },
    sub: { type: String, default: '' },
    tone: { type: String, default: '' },
  },
  setup: (props) => () =>
    h('div', { class: ['stat', props.tone] }, [
      h('div', { class: 'k' }, props.label),
      h('div', { class: 'v' }, [String(props.value), props.sub ? h('small', props.sub) : null]),
    ]),
})

// items: [{ k, v }]
export const KvList = defineComponent({
  name: 'KvList',
  props: { items: { type: Array, default: () => [] } },
  setup: (props) => () =>
    h('dl', { class: 'kv' },
      props.items.flatMap((item) => [
        h('dt', item.k),
        h('dd', item.v === null || item.v === undefined || item.v === '' ? '—' : item.v),
      ])),
})
