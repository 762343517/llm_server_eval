<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'
import { api } from '../api'
import { LogPanel, MiniChart, ProgressBar, StatTile, StatusBadge } from '../components/index.js'
import { compact, duration, int, isRunning, num, pct, shortFixed } from '../format'

const route = useRoute()
const router = useRouter()
const runId = route.params.id

const snap = ref(null)
const seriesPoints = ref([])
const records = ref([])
const recordTotal = ref(0)
const error = ref('')
const busy = ref(false)
const filters = ref({ limit: 200, onlyErrors: false })
const scenario = ref(null)
const showScenario = ref(false)

let timer = null
let tick = 0

const running = computed(() => isRunning(snap.value && snap.value.status))
const metrics = computed(() => (snap.value && (snap.value.summary || snap.value.stats)) || {})
const byLevel = computed(() => (snap.value && snap.value.by_level) || [])
const conv = computed(() => snap.value && snap.value.conv)
const isRamp = computed(() => byLevel.value.length > 0)

const chartSeries = [
  { key: 'rps', label: 'RPS', color: '#2563eb', digits: 2 },
  { key: 'ttft_mean_s', label: '首字延迟(s)', color: '#d97706', digits: 3 },
  { key: 'e2e_mean_s', label: '端到端(s)', color: '#16a34a', digits: 2 },
]

const loadRecords = async () => {
  try {
    const data = await api.stressRecords(runId, filters.value)
    records.value = data.records || []
    recordTotal.value = data.total || 0
  } catch (e) {
    error.value = e.message
  }
}

const loadSeries = async () => {
  try {
    const data = await api.stressSeries(runId, 60)
    seriesPoints.value = data.points || []
  } catch (e) {
    /* 曲线失败不影响主流程 */
  }
}

const loadSnap = async () => {
  try {
    snap.value = await api.stressRun(runId)
    error.value = ''
  } catch (e) {
    error.value = e.message
  }
}

const loadScenario = async () => {
  showScenario.value = !showScenario.value
  if (!showScenario.value || scenario.value) return
  try {
    const data = await api.stressScenario(runId)
    scenario.value = data.text
  } catch (e) {
    error.value = e.message
  }
}

const stopRun = async () => {
  if (!window.confirm('确认停止压测？llm-grill 子进程会被终止，已完成的结果保留。')) return
  busy.value = true
  try {
    await api.stressStop(runId)
    await loadSnap()
  } catch (e) {
    error.value = e.message
  } finally {
    busy.value = false
  }
}

const deleteRun = async () => {
  if (!window.confirm(`确认删除压测任务 ${runId}？场景与结果文件会一并删除，且不可恢复。`)) return
  busy.value = true
  try {
    await api.stressDelete(runId)
    router.push('/stress')
  } catch (e) {
    error.value = e.message
    busy.value = false
  }
}

onMounted(async () => {
  await loadSnap()
  await Promise.all([loadRecords(), loadSeries()])
  timer = setInterval(async () => {
    tick += 1
    if (!running.value) return
    await loadSnap()
    if (tick % 3 === 0) await loadSeries()
    if (tick % 5 === 0) await loadRecords()
  }, 2000)
})
onUnmounted(() => timer && clearInterval(timer))
</script>

<template>
  <div class="page-head">
    <div>
      <h1 class="row">
        压测任务 <span class="mono">{{ runId }}</span>
        <StatusBadge v-if="snap" :status="snap.status" :text="snap.status_text" />
      </h1>
      <div class="sub" v-if="snap">
        {{ snap.cfg.model }} · {{ snap.cfg.endpoint }} ·
        {{ snap.cfg.concurrency_mode === 'ramp' ? '阶梯加压' : '固定并发' }}
      </div>
    </div>
    <div class="actions">
      <RouterLink class="btn" to="/stress">返回列表</RouterLink>
      <RouterLink class="btn" :to="`/stress/${runId}/csv`">结果表格</RouterLink>
      <a class="btn" :href="`/api/stress/runs/${runId}/download.csv`">下载 CSV</a>
      <button class="btn" @click="loadScenario">
        {{ showScenario ? '收起场景' : '查看场景 YAML' }}
      </button>
      <button v-if="running" class="btn danger" :disabled="busy" @click="stopRun">停止</button>
      <button class="btn danger" :disabled="busy" @click="deleteRun">删除</button>
    </div>
  </div>

  <div v-if="error" class="alert bad">{{ error }}</div>
  <div v-if="snap && snap.error" class="alert bad">失败原因：{{ snap.error }}</div>
  <div v-if="snap && metrics.error_count" class="alert warn">
    <div>
      有 {{ metrics.error_count }} 个请求失败（成功率 {{ pct(metrics.success_rate) }}），错误分布：
    </div>
    <ul style="margin: 6px 0 0; padding-left: 18px">
      <li v-for="(item, i) in metrics.errors || []" :key="i">
        {{ item.count }} 次 · {{ item.message }}
      </li>
    </ul>
  </div>

  <section v-if="snap" class="card">
    <h2>运行进度</h2>
    <div class="muted">
      {{ snap.progress.stage }} · {{ snap.progress.text }} · 开始于 {{ snap.started_at_str }}
      <span v-if="snap.finished_at_str"> · 结束于 {{ snap.finished_at_str }}</span>
      · 预计 {{ int(snap.expected) }} 次请求
    </div>
    <ProgressBar
      :pct="snap.progress.pct"
      :status="snap.status"
      :text="`${(snap.progress.pct || 0).toFixed(1)}% · ${duration(snap.duration_s)}`"
    />
    <div class="stats mt">
      <StatTile label="吞吐 RPS" :value="num(metrics.requests_per_second, 2)" />
      <StatTile
        label="成功率"
        :value="pct(metrics.success_rate)"
        :tone="metrics.success_rate >= 0.99 ? 'good' : metrics.error_count ? 'bad' : ''"
      />
      <StatTile label="总请求" :value="int(metrics.total_requests)" :sub="`失败 ${int(metrics.error_count)}`" />
      <StatTile label="首字延迟 均值" :value="shortFixed(metrics.ttft_mean_s, 3)" sub="s" />
      <StatTile label="首字延迟 P95" :value="shortFixed(metrics.ttft_p95_s, 3)" sub="s" />
      <StatTile label="端到端 均值" :value="shortFixed(metrics.e2e_mean_s, 2)" sub="s" />
      <StatTile label="端到端 P95" :value="shortFixed(metrics.e2e_p95_s, 2)" sub="s" />
      <StatTile label="TPOT 均值" :value="shortFixed(metrics.tpot_mean_s, 4)" sub="s/token" />
      <StatTile label="单请求输出" :value="num(metrics.tokens_per_second_mean, 1)" sub="tokens/s" />
      <StatTile label="集群输出" :value="num(metrics.total_tokens_per_second, 1)" sub="tokens/s" />
      <StatTile label="输出 token" :value="compact(metrics.completion_tokens_total)" />
      <StatTile label="压测总时长" :value="duration(metrics.total_duration_s)" />
    </div>
  </section>

  <section class="card">
    <h2>实时曲线</h2>
    <p class="hint">按时间分桶（每桶最多 60 个点）{{ running ? ' · 运行中每 6 秒刷新' : '' }}</p>
    <MiniChart :points="seriesPoints" :series="chartSeries" />
  </section>

  <section v-if="isRamp" class="card">
    <h2>阶梯并发对比</h2>
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th class="num">并发</th>
            <th class="num">请求数</th>
            <th class="num">成功率</th>
            <th class="num">RPS</th>
            <th class="num">TTFT 均值</th>
            <th class="num">TTFT P95</th>
            <th class="num">端到端 均值</th>
            <th class="num">端到端 P95</th>
            <th class="num">单请求 tokens/s</th>
            <th class="num">失败数</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in byLevel" :key="row.level">
            <td class="num">{{ row.level }}</td>
            <td class="num">{{ int(row.total_requests) }}</td>
            <td class="num">{{ pct(row.success_rate) }}</td>
            <td class="num">{{ num(row.requests_per_second, 2) }}</td>
            <td class="num">{{ shortFixed(row.ttft_mean_s, 3) }}</td>
            <td class="num">{{ shortFixed(row.ttft_p95_s, 3) }}</td>
            <td class="num">{{ shortFixed(row.e2e_mean_s, 2) }}</td>
            <td class="num">{{ shortFixed(row.e2e_p95_s, 2) }}</td>
            <td class="num">{{ num(row.tokens_per_second_mean, 1) }}</td>
            <td class="num">{{ int(row.error_count) }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </section>

  <section v-if="conv" class="card">
    <h2>多轮会话指标</h2>
    <p class="hint">观察到模型把上一轮回答作为上下文，用于评估长上下文与 KV 缓存复用</p>
    <div class="stats">
      <StatTile
        label="上下文增长系数"
        :value="num(conv.context_growth_factor, 2)"
        sub="末轮/首轮端到端"
        :tone="conv.context_growth_factor > 1.5 ? 'bad' : 'good'"
      />
      <StatTile
        label="KV 缓存命中率"
        :value="conv.kv_cache_hit_rate_mean === null || conv.kv_cache_hit_rate_mean === undefined ? '—' : pct(conv.kv_cache_hit_rate_mean)"
      />
      <StatTile
        label="KV 缓存占用"
        :value="conv.kv_cache_usage_mean === null || conv.kv_cache_usage_mean === undefined ? '—' : pct(conv.kv_cache_usage_mean)"
      />
      <StatTile label="轮次数" :value="conv.ttft_by_turn.length" />
    </div>
    <div class="table-wrap mt">
      <table>
        <thead>
          <tr>
            <th class="num">轮次</th>
            <th class="num">TTFT 均值(s)</th>
            <th class="num">端到端 均值(s)</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(ttft, i) in conv.ttft_by_turn" :key="i">
            <td class="num">{{ i + 1 }}</td>
            <td class="num">{{ shortFixed(ttft, 3) }}</td>
            <td class="num">{{ shortFixed(conv.e2e_by_turn[i], 2) }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </section>

  <section class="card">
    <h2>逐请求明细</h2>
    <p class="hint">共 {{ recordTotal }} 条{{ running ? ' · 运行中每 10 秒刷新' : '' }}</p>
    <div class="filters">
      <label class="check">
        <input
          v-model="filters.onlyErrors"
          type="checkbox"
          @change="loadRecords"
        />
        <span>只看失败请求</span>
      </label>
      <select v-model.number="filters.limit" @change="loadRecords">
        <option :value="100">显示 100 条</option>
        <option :value="200">显示 200 条</option>
        <option :value="500">显示 500 条</option>
        <option :value="2000">显示 2000 条</option>
      </select>
      <button class="btn sm" @click="loadRecords">刷新</button>
    </div>
    <div v-if="!records.length" class="empty">暂无请求记录</div>
    <div v-else class="table-wrap tall">
      <table>
        <thead>
          <tr>
            <th>开始时间</th>
            <th class="num">并发档</th>
            <th class="num">用户</th>
            <th class="num">轮次</th>
            <th class="center">结果</th>
            <th class="num">TTFT s</th>
            <th class="num">TPOT s</th>
            <th class="num">端到端 s</th>
            <th class="num">prompt</th>
            <th class="num">输出</th>
            <th class="num">tokens/s</th>
            <th>错误</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(row, i) in records" :key="i">
            <td class="mono nowrap">{{ row.timestamp_start }}</td>
            <td class="num">{{ row.concurrent_users_level }}</td>
            <td class="num">{{ row.user_id }}</td>
            <td class="num">{{ row.turn }}</td>
            <td class="center">
              <span class="badge" :class="row.success ? 'ok' : 'bad'">
                {{ row.success ? '成功' : '失败' }}
              </span>
            </td>
            <td class="num">{{ shortFixed(row.ttft_s, 3) }}</td>
            <td class="num">{{ shortFixed(row.tpot_s, 4) }}</td>
            <td class="num">{{ shortFixed(row.e2e_latency_s, 2) }}</td>
            <td class="num">{{ int(row.prompt_tokens) }}</td>
            <td class="num">{{ int(row.completion_tokens) }}</td>
            <td class="num">{{ num(row.tokens_per_second, 1) }}</td>
            <td>
              <span class="clip" :title="row.error || ''">
                {{ row.error || '—' }}
              </span>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </section>

  <section v-if="showScenario" class="card">
    <h2>场景文件（api_key 已脱敏）</h2>
    <details class="scenario" open>
      <summary>scenario.yaml</summary>
      <pre>{{ scenario || '加载中…' }}</pre>
    </details>
  </section>

  <section class="card">
    <h2>运行日志</h2>
    <LogPanel :logs="(snap && snap.logs) || []" />
  </section>
</template>
