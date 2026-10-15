<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'
import { api } from '../api'
import { KvList, LogPanel, MiniChart, ProgressBar, StatTile, StatusBadge } from '../components/index.js'
import { compact, duration, int, isRunning, ms, num, pct, shortFixed } from '../format'

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
const args = ref(null)
const showArgs = ref(false)
const files = ref([])
const preview = ref(null)

let timer = null
let tick = 0

const running = computed(() => isRunning(snap.value && snap.value.status))
const metrics = computed(() => (snap.value && (snap.value.summary || snap.value.stats)) || {})
const byLevel = computed(() => (snap.value && snap.value.by_level) || [])
const conv = computed(() => snap.value && snap.value.conv)
const isRamp = computed(() => byLevel.value.length > 0)
const cfg = computed(() => (snap.value && snap.value.cfg) || {})

// 快照里的请求数：新配置是 numbers（= evalscope --number，与并发档位同序）；早期快照只有
// iterations（每个并发的请求数），按并发数换算回 --number 显示。
const cfgNumbers = (c) => {
  if (!c) return []
  if (Array.isArray(c.numbers) && c.numbers.length) return c.numbers
  const legacy = c.iterations
  if (legacy === null || legacy === undefined || legacy === '') return []
  const ramp = c.concurrency_mode
    ? c.concurrency_mode === 'ramp'
    : Array.isArray(c.ramp_levels) && c.ramp_levels.length > 0
  const users = ramp ? (c.ramp_levels || []) : [c.concurrent_users]
  return users.map((u) => (Number(u) || 0) * Number(legacy))
}

const cfgRows = computed(() => {
  const c = cfg.value
  const text = (v) => (v === null || v === undefined || v === '' ? '—' : String(v))
  const isLong = c.prompt_mode === 'long'
  // 兼容早期快照：没有 concurrency_mode 时按 ramp_levels 是否为空判断
  const isRampCfg = c.concurrency_mode
    ? c.concurrency_mode === 'ramp'
    : Array.isArray(c.ramp_levels) && c.ramp_levels.length > 0
  const rows = [
    { k: '任务名称', v: text(c.name) },
    { k: '说明', v: text(c.description) },
    { k: '服务端点', v: text(c.endpoint) },
    { k: 'API Key', v: text(c.api_key) },
    { k: '模型名', v: text(c.model) },
    { k: '请求模式', v: isLong ? '长序列 + 固定前缀（Prefix Cache）' : '自定义会话提问' },
  ]
  if (isLong) {
    const custom = Boolean(c.prefix_content_chars)
    rows.push(
      { k: '分词器路径', v: text(c.tokenizer_path) },
      { k: '序列长度', v: c.seq_length ? `${c.seq_length} tokens` : '—' },
      { k: '输出长度', v: c.output_length ? `${c.output_length} tokens` : '—' },
      {
        k: 'Prefix Cache 命中率',
        v: custom ? '按前缀内容自动' : c.prefix_cache_ratio === null ? '—' : pct(c.prefix_cache_ratio),
      },
      {
        k: '固定前缀长度',
        v: custom
          ? '自动（序列长度 − 提问长度）'
          : c.prefix_length === null || c.prefix_length === undefined
            ? '—'
            : `${c.prefix_length} tokens`,
      },
      {
        k: '固定前缀内容',
        v: custom ? `已填写（${c.prefix_content_chars} 字符）` : '未填写（使用共享随机前缀）',
      },
      { k: '预热请求数', v: text(c.warmup_num) },
      { k: '强制输出长度', v: c.ignore_eos ? '是（ignore_eos）' : '否' },
      // random 数据集按 token 随机生成请求内容，提问内容只在固定前缀内容模式下生效
      {
        k: '提问内容',
        v: custom
          ? (String(c.prompt || '').trim() || '未填写（使用默认提问）')
          : '不适用（random 数据集按 token 随机生成）',
      },
    )
  } else {
    rows.push(
      { k: 'max_tokens', v: text(c.max_tokens) },
      { k: '系统提示词', v: text(c.system_prompt) },
      { k: '提问内容', v: text(c.prompt) },
      { k: '对话轮数', v: text(c.turns) },
      { k: '会话唯一前缀', v: c.unique_prefix ? '是' : '否' },
    )
  }
  rows.push(
    { k: 'temperature', v: text(c.temperature) },
    { k: 'top_p', v: text(c.top_p) },
    { k: 'top_k', v: c.top_k === -1 ? '-1（不发送）' : text(c.top_k) },
    { k: '请求超时', v: c.timeout === null || c.timeout === undefined ? '—' : `${c.timeout}s` },
    { k: '加压方式', v: isRampCfg ? '阶梯加压' : '固定并发' },
  )
  // 两种加压方式的生效字段不同：concurrent_users 在阶梯模式下是表单残留值，
  // ramp_levels 在固定并发下同理，因此各自只展示本轮真正生效的那组。
  // 请求数口径与 evalscope 一致：numbers 就是 --number 列表（早期快照只有 iterations）
  const counts = cfgNumbers(c)
  if (isRampCfg) {
    rows.push(
      {
        k: '并发档位（--parallel）',
        v: Array.isArray(c.ramp_levels) && c.ramp_levels.length ? c.ramp_levels.join(' / ') : '—',
      },
      {
        k: '档位间隔',
        v: c.ramp_pause_seconds === null || c.ramp_pause_seconds === undefined
          ? '—'
          : `${c.ramp_pause_seconds}s`,
      },
      {
        k: isLong ? '各档请求数（--number）' : '各档会话数（--number）',
        v: counts.length ? counts.join(' / ') : '—',
      },
    )
  } else {
    rows.push(
      { k: '并发数（--parallel）', v: text(c.concurrent_users) },
      {
        k: isLong ? '请求数（--number）' : '会话数（--number）',
        v: counts.length ? text(counts[0]) : '—',
      },
    )
  }
  return rows
})

// evalscope 只在解析到流的**非空增量文本**后才记 TTFT，未记到时落库为 0。
// 直接显示 0 ms 会被读成"首字瞬时返回"，这里如实标为无数据（聚合值口径不变，
// 与 benchmark.log 的 Avg TTFT 一致）。
const ttftText = (row) => (Number(row.ttft_s) > 0 ? ms(row.ttft_s) : '—')

// 曲线与表内口径保持一致：接口给的是秒，绘图前换算成毫秒
const chartPoints = computed(() =>
  seriesPoints.value.map((p) => ({ ...p, ttft_ms: (Number(p.ttft_mean_s) || 0) * 1000 })),
)

const chartSeries = [
  { key: 'rps', label: 'RPS', color: '#2563eb', digits: 2 },
  { key: 'ttft_ms', label: '首字延迟(ms)', color: '#d97706', digits: 1 },
  { key: 'e2e_mean_s', label: '端到端(s)', color: '#16a34a', digits: 2 },
]

// ------------------------------------------------------------ 原始产物
const fileSize = (value) => {
  const n = Number(value) || 0
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`
  return `${(n / 1024 / 1024).toFixed(2)} MB`
}

const fileTime = (value) => {
  const n = Number(value)
  if (!n) return '—'
  return new Date(n * 1000).toLocaleString('zh-CN', { hour12: false })
}

const fileUrl = (file, download = false) =>
  api.stressFileUrl(runId, file.path, { download })

const filesError = ref('')
const loadFiles = async () => {
  try {
    const data = await api.stressFiles(runId)
    files.value = data.files || []
    filesError.value = ''
  } catch (e) {
    filesError.value = e.message        // 接口异常也不能影响页面其余部分
  }
}

const PREVIEW_LIMIT = 400000

const openFile = async (file) => {
  preview.value = { ...file, text: '', loading: file.kind !== 'html', url: fileUrl(file) }
  if (file.kind === 'html') return
  try {
    const res = await fetch(fileUrl(file))
    if (!res.ok) throw new Error(`HTTP ${res.status}`)
    const text = await res.text()
    preview.value = {
      ...preview.value,
      loading: false,
      text:
        text.length > PREVIEW_LIMIT
          ? `${text.slice(0, PREVIEW_LIMIT)}\n\n…（文件较大，已截断显示，完整内容请下载）`
          : text,
    }
  } catch (e) {
    preview.value = null
    error.value = `读取 ${file.name} 失败：${e.message}`
  }
}

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

const loadArgs = async () => {
  showArgs.value = !showArgs.value
  if (!showArgs.value || args.value) return
  try {
    const data = await api.stressArgs(runId)
    args.value = data.text
  } catch (e) {
    error.value = e.message
  }
}

const stopRun = async () => {
  if (!window.confirm('确认停止压测？evalscope 子进程会被终止，已完成的结果保留。')) return
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
  if (!window.confirm(`确认删除压测任务 ${runId}？数据集与结果文件会一并删除，且不可恢复。`)) return
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
  await Promise.all([loadRecords(), loadSeries(), loadFiles()])
  timer = setInterval(async () => {
    tick += 1
    if (!running.value) return
    await loadSnap()
    if (tick % 3 === 0) await loadSeries()
    if (tick % 5 === 0) {
      await loadRecords()
      await loadFiles()          // 跑完后 evalscope 才写出报告，运行中一并刷新
    }
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
      <button class="btn" @click="loadArgs">
        {{ showArgs ? '收起运行参数' : '查看运行参数' }}
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
      <StatTile label="首字延迟 均值" :value="ms(metrics.ttft_mean_s)" sub="ms" />
      <StatTile label="首字延迟 P95" :value="ms(metrics.ttft_p95_s)" sub="ms" />
      <StatTile label="端到端 均值" :value="shortFixed(metrics.e2e_mean_s, 2)" sub="s" />
      <StatTile label="端到端 P95" :value="shortFixed(metrics.e2e_p95_s, 2)" sub="s" />
      <StatTile label="TPOT 均值" :value="ms(metrics.tpot_mean_s)" sub="ms/token" />
      <StatTile label="单请求输出" :value="num(metrics.tokens_per_second_mean, 1)" sub="tokens/s" />
      <StatTile
        label="输出吞吐"
        :value="num(metrics.total_tokens_per_second, 1)"
        sub="tokens/s · 仅输出"
      />
      <StatTile
        label="总吞吐"
        :value="num(metrics.overall_tokens_per_second, 1)"
        sub="tokens/s · 含输入"
      />
      <StatTile
        label="输出 token"
        :value="compact(metrics.completion_tokens_total)"
        sub="全部请求合计"
      />
      <StatTile label="压测总时长" :value="duration(metrics.total_duration_s)" />
    </div>
  </section>

  <section v-if="snap" class="card">
    <h2>配置参数</h2>
    <p class="hint">本次压测提交的运行配置（API Key 已脱敏）</p>
    <KvList :items="cfgRows" />
  </section>

  <section class="card">
    <h2>实时曲线</h2>
    <p class="hint">按时间分桶（每桶最多 60 个点）{{ running ? ' · 运行中每 6 秒刷新' : '' }}</p>
    <MiniChart :points="chartPoints" :series="chartSeries" />
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
            <th class="num">TTFT 均值(ms)</th>
            <th class="num">TTFT P95(ms)</th>
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
            <td class="num">{{ ms(row.ttft_mean_s) }}</td>
            <td class="num">{{ ms(row.ttft_p95_s) }}</td>
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
    <p class="hint">模型将上一轮回答作为上下文，用于评估长上下文与 KV 缓存复用</p>
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
            <th class="num">TTFT 均值(ms)</th>
            <th class="num">端到端 均值(s)</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(ttft, i) in conv.ttft_by_turn" :key="i">
            <td class="num">{{ i + 1 }}</td>
            <td class="num">{{ ms(ttft) }}</td>
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
            <th class="num">TTFT ms</th>
            <th class="num">TPOT ms</th>
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
            <td class="num">{{ ttftText(row) }}</td>
            <td class="num">{{ ms(row.tpot_s) }}</td>
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

  <section v-if="showArgs" class="card">
    <h2>运行参数（api_key 已脱敏）</h2>
    <p class="hint">evalscope 命令行与本次运行参数，原文见任务目录 args.json。</p>
    <details class="scenario" open>
      <summary>args.json</summary>
      <pre>{{ args || '加载中…' }}</pre>
    </details>
  </section>

  <section class="card">
    <h2>原始产物</h2>
    <p class="hint">
      evalscope 直接生成的报告文件，可在线查看或下载完整原文；可视化报告为交互式 HTML，
      图表脚本取自 CDN，离线环境图表不显示，下载到本地打开即可
    </p>
    <div v-if="filesError" class="empty">读取原始产物清单失败：{{ filesError }}</div>
    <div v-else-if="!files.length" class="empty">
      暂无可用的原始产物（任务未跑完或文件已清理）
    </div>
    <div v-else class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>文件名</th>
            <th>说明</th>
            <th class="num">大小</th>
            <th class="num">修改时间</th>
            <th class="center">操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="file in files" :key="file.path">
            <td class="mono nowrap">{{ file.name }}</td>
            <td>{{ file.label }}</td>
            <td class="num nowrap">{{ fileSize(file.size) }}</td>
            <td class="num nowrap">{{ fileTime(file.mtime) }}</td>
            <td class="center nowrap">
              <button class="btn sm" @click="openFile(file)">查看</button>
              <a class="btn sm" :href="fileUrl(file, true)">下载</a>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <div v-if="preview" class="file-preview mt">
      <div class="preview-head">
        <span class="mono">{{ preview.name }}</span>
        <span class="muted">{{ preview.label }}</span>
        <span class="preview-actions">
          <a class="btn sm" :href="fileUrl(preview, true)">下载</a>
          <button class="btn sm" @click="preview = null">关闭</button>
        </span>
      </div>
      <div v-if="preview.kind === 'html'" class="preview-frame">
        <iframe :src="preview.url" title="perf_report.html"></iframe>
      </div>
      <pre v-else-if="preview.loading">加载中…</pre>
      <pre v-else>{{ preview.text }}</pre>
    </div>
  </section>

  <section class="card">
    <h2>运行日志</h2>
    <LogPanel :logs="(snap && snap.logs) || []" />
  </section>
</template>
