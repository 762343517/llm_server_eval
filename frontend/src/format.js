// 展示层格式化：数值、百分比、耗时、状态色
export const num = (value, digits = 2) => {
  const n = Number(value)
  if (value === null || value === undefined || value === '' || Number.isNaN(n)) return '—'
  return n.toFixed(digits)
}

export const int = (value) => {
  const n = Number(value)
  if (value === null || value === undefined || value === '' || Number.isNaN(n)) return '—'
  return String(Math.round(n))
}

export const pct = (value, digits = 1) => {
  const n = Number(value)
  if (value === null || value === undefined || Number.isNaN(n)) return '—'
  return `${(n * 100).toFixed(digits)}%`
}

export const shortFixed = (value, digits = 3) => {
  const n = Number(value)
  if (value === null || value === undefined || Number.isNaN(n)) return '—'
  return n.toFixed(digits)
}

// 秒 → 1h 02m / 03m 12s / 12.3s
export const duration = (seconds) => {
  const s = Number(seconds)
  if (!s || s < 0 || Number.isNaN(s)) return '—'
  if (s < 60) return `${s.toFixed(1)}s`
  const m = Math.floor(s / 60)
  const rest = Math.round(s % 60)
  if (m < 60) return `${m}m ${String(rest).padStart(2, '0')}s`
  const h = Math.floor(m / 60)
  return `${h}h ${String(m % 60).padStart(2, '0')}m`
}

// 大数字：12345 → 12.3k
export const compact = (value) => {
  const n = Number(value)
  if (value === null || value === undefined || Number.isNaN(n)) return '—'
  if (Math.abs(n) >= 1e9) return `${(n / 1e9).toFixed(2)}B`
  if (Math.abs(n) >= 1e6) return `${(n / 1e6).toFixed(2)}M`
  if (Math.abs(n) >= 1e3) return `${(n / 1e3).toFixed(1)}k`
  return String(n)
}

const STATUS_KIND = {
  running: 'busy',
  succeeded: 'ok',
  failed: 'bad',
  stopped: 'warn',
  interrupted: 'warn',
}

export const statusKind = (status) => STATUS_KIND[status] || 'off'
export const isRunning = (status) => status === 'running'

// 长文本截断（表格中展示模型回答）
export const clip = (text, max = 120) => {
  const s = text === null || text === undefined ? '' : String(text)
  return s.length > max ? `${s.slice(0, max)}…` : s
}
