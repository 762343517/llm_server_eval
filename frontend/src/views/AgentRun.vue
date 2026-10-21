<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'
import { api } from '../api'
import { Card, KvList, LogPanel, ProgressBar, StatTile, StatusBadge } from '../components/index.js'
import { compact, duration, int, isActive, isRunning, num, pct, shortFixed } from '../format'

const route = useRoute()
const router = useRouter()
const runId = route.params.id

const snap = ref(null)
// 子任务看板：等待 / 运行中 / 完成 / 异常退出（含失败原因分类）
const subtasks = ref([])
const subCounts = ref({})
const openTask = ref('')          // 当前展开详情的子任务
const taskLogs = ref([])          // 展开子任务的实时日志（累积）
const logOffset = ref(-1)         // 日志增量偏移，-1 表示从尾部开始
const error = ref('')
const busy = ref(false)
const filters = ref({ state: '', kind: '', q: '', limit: 200 })
let timer = null
let tick = 0
let logBusy = false

const running = computed(() => isRunning(snap.value && snap.value.status))
// 排队中同样要保持轮询：等本任务开跑后页面自动切到实时进度
const active = computed(() => isActive(snap.value && snap.value.status))
const queueAhead = computed(() => Math.max(0, ((snap.value && snap.value.queue_position) || 1) - 1))
const agent = computed(() => (snap.value && snap.value.agent) || {})
const summary = computed(() => (snap.value && snap.value.summary) || {})
const cfg = computed(() => (snap.value && snap.value.cfg) || {})
// 通过率分母 = 计分任务数（环境/超时失败不计入），旧 run 没有该字段时退回总记录数
const scoredCount = computed(() => {
  const s = summary.value
  return s.num_scored === undefined || s.num_scored === null ? s.num_records : s.num_scored
})
const excludedCount = computed(() =>
  (summary.value.num_excluded === undefined || summary.value.num_excluded === null
    ? 0 : Number(summary.value.num_excluded)))
const passSub = computed(() => (excludedCount.value
  ? `计分 ${int(scoredCount.value)}，剔除环境/超时 ${int(excludedCount.value)}`
  : ''))

const cfgRows = computed(() => {
  const c = cfg.value
  const text = (v) => (v === null || v === undefined || v === '' ? '—' : String(v))
  return [
    { k: '任务集', v: text(c.data) },
    { k: '执行 agent', v: text(c.agent) },
    { k: '被测模型', v: text(c.model) },
    { k: '模型调用方式', v: text(c.model_class) },
    { k: '被测端点', v: text(c.endpoint) },
    { k: '被测 API Key', v: text(c.api_key) },
    { k: '指定任务', v: text(c.tasks) },
    { k: '任务数', v: c.tasks ? '已指定任务，忽略' : (c.limit ? String(c.limit) : '全部') },
    { k: '抽样种子', v: c.seed ? String(c.seed) : '按目录顺序' },
    { k: '并发任务数', v: text(c.concurrency) },
    { k: '任务超时倍数', v: text(c.timeout_multiplier) },
    { k: '判定超时倍数', v: text(c.verifier_timeout_multiplier) },
  ]
})

// 子任务状态由后端归一化：waiting / running / done / error
const STATE_TEXT = { waiting: '等待', running: '运行中', done: '完成', error: '异常退出' }
const rowClass = (row) => {
  if (row.state === 'error') return 'row-bad'
  if (row.state === 'done') return row.status === 'resolved' ? 'row-ok' : 'row-warn'
  return ''
}
const badgeClass = (row) => ({
  ok: row.state === 'done' && row.status === 'resolved',
  warn: row.state === 'done' && row.status !== 'resolved',
  bad: row.state === 'error',
  busy: row.state === 'running',
  off: row.state === 'waiting',
})
// 失败原因分类（后端 failure_kind），用于筛选与表格标注
const KIND_TEXT = {
  timeout: '超时异常',
  instruction: '指令异常',
  environment: '环境异常',
  unresolved: '判分未通过',
  unknown: '未分类异常',
}

const stateOptions = computed(() =>
  [...new Set(subtasks.value.map((r) => r.state).filter(Boolean))])
const kindOptions = computed(() =>
  [...new Set(subtasks.value.map((r) => r.failure_kind).filter(Boolean))])
// 汇总优先用后端统计；旧 run 缺该字段时按子任务列表兜底
const kindRows = computed(() => {
  const fromSummary = summary.value.by_failure_kind
  if (Array.isArray(fromSummary) && fromSummary.length) return fromSummary
  const counts = {}
  for (const row of subtasks.value) {
    if (row.failure_kind) counts[row.failure_kind] = (counts[row.failure_kind] || 0) + 1
  }
  return Object.entries(KIND_TEXT).map(([kind, text]) => ({ kind, text, count: counts[kind] || 0 }))
})

const visibleTasks = computed(() => {
  let rows = subtasks.value
  if (filters.value.state) rows = rows.filter((r) => r.state === filters.value.state)
  if (filters.value.kind) rows = rows.filter((r) => r.failure_kind === filters.value.kind)
  const needle = String(filters.value.q || '').trim().toLowerCase()
  if (needle) {
    rows = rows.filter((r) => [r.task, r.title, r.category, r.error,
      r.failure_kind_text, r.failure_type]
      .some((v) => String(v || '').toLowerCase().includes(needle)))
  }
  return rows.slice(0, filters.value.limit)
})

const detailRows = (row) => {
  const text = (v) => (v === null || v === undefined || v === '' ? '—' : String(v))
  return [
    { k: '任务', v: row.task },
    { k: '标题', v: text(row.title) },
    { k: '类别', v: text(row.category) },
    { k: '状态', v: row.state_text },
    { k: '结果', v: text(row.result_text) },
    {
      k: '计入通过率',
      v: row.state === 'waiting' || row.state === 'running'
        ? '—'
        : (row.failure_kind === 'timeout' || row.failure_kind === 'environment'
          ? '否（环境/超时不计入）' : '是'),
    },
    {
      k: '失败原因',
      v: row.state === 'error'
        ? `${row.failure_kind_text || '未分类异常'}${row.failure_type ? `（${row.failure_type}）` : ''}`
        : '—',
    },
    { k: 'reward', v: text(row.reward) },
    { k: '步数', v: text(row.steps) },
    {
      k: '耗时',
      v: row.elapsed_s === null || row.elapsed_s === undefined ? '—' : duration(row.elapsed_s),
    },
    {
      k: 'token（入/出/缓存）',
      v: `${int(row.input_tokens)} / ${int(row.output_tokens)} / ${int(row.cache_tokens)}`,
    },
    { k: '异常信息', v: text(row.error) },
    { k: '判定输出', v: text(row.verifier_tail) },
    { k: '产物目录', v: text(row.trial_dir) },
  ]
}

// 产物：测试输出 / 模型补丁 / 任务日志（后端只允许读取该任务目录内的文件）
const artifactUrl = (row, rel) =>
  api.agentFileUrl(runId, `${row.trial_dir}/${rel}`, { download: false })

const groupRows = (group) =>
  Object.entries(group || {}).map(([k, v]) => [k, v])

const loadSubtasks = async () => {
  try {
    const data = await api.agentSubtasks(runId)
    subtasks.value = data.items || []
    subCounts.value = data.counts || {}
  } catch (e) {
    error.value = e.message
  }
}

const resetTaskLog = () => {
  taskLogs.value = []
  logOffset.value = -1
}

// 首次取日志尾部，之后按 offset 增量追加；等待中的任务还没有日志，静默跳过
const pullTaskLog = async (reset = false) => {
  const task = openTask.value
  if (!task || logBusy) return
  if (reset) resetTaskLog()
  logBusy = true
  try {
    const data = await api.agentTaskLog(runId, task, logOffset.value)
    if (openTask.value !== task) return
    if (data.reset) taskLogs.value = []
    const lines = String(data.text || '').split('\n').filter((x) => x)
    if (lines.length) {
      taskLogs.value = [...taskLogs.value, ...lines.map((msg) => ({ t: '', msg }))]
        .slice(-1500)
    }
    logOffset.value = data.next || 0
  } catch (e) {
    // 任务尚未开始或产物已清理：保留已有内容即可
  } finally {
    logBusy = false
  }
}

const toggleTask = async (row) => {
  if (openTask.value === row.task) {
    openTask.value = ''
    resetTaskLog()
    return
  }
  openTask.value = row.task
  resetTaskLog()
  await pullTaskLog(true)
}

const loadSnap = async () => {
  try {
    snap.value = await api.agentRun(runId)
    error.value = ''
  } catch (e) {
    error.value = e.message
  }
}

const tickLoop = async () => {
  tick += 1
  await loadSnap()
  // 子任务状态每 2s 刷新一次；全部结束后只需低频兜底
  if (tick % 2 === 1 || !running.value) await loadSubtasks()
  await pullTaskLog()
}

const stopRun = async () => {
  if (!window.confirm('确认停止该评测任务？pier 与其容器会被结束，已完成的部分结果会保留。')) return
  busy.value = true
  try {
    await api.agentStop(runId)
    await loadSnap()
  } catch (e) {
    error.value = e.message
  } finally {
    busy.value = false
  }
}

const deleteRun = async () => {
  if (!window.confirm(`确认删除任务 ${runId}？容器产物与结果文件会一并删除，且不可恢复。`)) return
  busy.value = true
  try {
    await api.agentDelete(runId)
    router.push('/agent')
  } catch (e) {
    error.value = e.message
    busy.value = false
  }
}

onMounted(async () => {
  await loadSnap()
  await loadSubtasks()
  timer = setInterval(() => {
    if (active.value) tickLoop()
  }, 2000)
})
onUnmounted(() => {
  if (timer) clearInterval(timer)
})
</script>

<template>
  <div class="page-head">
    <div>
      <h1 class="row">
        Agent 评测 <span class="mono">{{ runId }}</span>
        <StatusBadge v-if="snap" :status="snap.status" :text="snap.status_text" />
      </h1>
      <div class="sub" v-if="snap">
        {{ snap.cfg.agent }} · 模型 {{ snap.cfg.model || '—' }} · 任务集 {{ snap.cfg.data }}
      </div>
    </div>
    <div class="actions">
      <RouterLink class="btn" to="/agent">返回列表</RouterLink>
      <RouterLink class="btn" :to="`/agent/${runId}/csv`">结果表格</RouterLink>
      <a class="btn" :href="`/api/agent/runs/${runId}/download.csv`">下载 CSV</a>
      <button v-if="active" class="btn danger" :disabled="busy" @click="stopRun">
        {{ running ? '停止' : '取消排队' }}
      </button>
      <button class="btn danger" :disabled="busy" @click="deleteRun">删除</button>
    </div>
  </div>

  <div v-if="error" class="alert bad">{{ error }}</div>
  <div v-if="snap && snap.status === 'queued'" class="alert warn">
    该任务正在全局测试队列中排队（前面还有 {{ queueAhead }} 个任务）。
    前序任务完全结束后才会开始，两次任务之间会留出资源释放间隔。
  </div>
  <div v-if="snap && snap.error" class="alert bad">失败原因：{{ snap.error }}</div>
  <div v-if="snap && summary.stopped" class="alert warn">
    该任务在完成前被停止，汇总仅覆盖已完成的部分。
  </div>
  <div v-if="snap && snap.status === 'running'" class="alert info">
    容器内 agent 单步耗时较长，进度按「已完成任务数」统计；等待期间可查看运行日志。
  </div>

  <Card v-if="snap" title="运行进度">
    <div class="muted">
      {{ snap.progress.stage }} · {{ snap.progress.text }}开始于 {{ snap.started_at_str }}
      <span v-if="snap.finished_at_str"> · 结束于 {{ snap.finished_at_str }}</span>
    </div>
    <ProgressBar
      :pct="snap.progress.pct"
      :status="snap.status"
      :text="`${(snap.progress.pct || 0).toFixed(1)}% · ${duration(snap.duration_s)}`"
    />
    <div class="stats mt">
      <StatTile
        label="通过率 (pass@1)"
        :value="summary.pass_at_1 === undefined || summary.pass_at_1 === null ? '—' : pct(summary.pass_at_1)"
        :sub="passSub"
        :tone="summary.pass_at_1 >= 0.5 ? 'good' : ''"
      />
      <StatTile
        label="通过任务"
        :value="int(summary.resolved)"
        :sub="`/ ${int(scoredCount)}`"
        :tone="summary.resolved ? 'good' : ''"
      />
      <StatTile label="未通过" :value="int(summary.unresolved)" />
      <StatTile
        label="异常"
        :value="int(summary.errors)"
        :sub="excludedCount ? `环境/超时 ${int(excludedCount)} 不计入` : ''"
        :tone="summary.errors ? 'bad' : ''"
      />
      <StatTile label="计划任务" :value="int(agent.total)" :sub="`已完成 ${int(agent.done)}`" />
      <StatTile label="平均步数" :value="num(summary.avg_steps, 1)" sub="步/任务" />
      <StatTile label="tokens(入/出)" :value="`${compact(summary.input_tokens)} / ${compact(summary.output_tokens)}`" />
      <StatTile label="耗时" :value="duration(snap.duration_s)" :sub="summary.avg_elapsed_s ? `平均 ${duration(summary.avg_elapsed_s)}` : ''" />
    </div>
    <p class="hint mt">
      pier {{ summary.pier_version || '—' }} · docker 权限 {{ summary.docker_mode || '—' }} ·
      egress {{ summary.egress || '—' }} · 任务产物：
      <span class="mono">{{ summary.raw || '—' }}</span>
    </p>
  </Card>

  <Card v-if="snap" title="配置参数">
    <p class="hint">本次评测提交的运行配置（API Key 已脱敏）</p>
    <KvList :items="cfgRows" />
  </Card>

  <Card title="逐任务结果">
    <p class="hint">
      共 {{ subtasks.length }} 个子任务
      <span v-if="subCounts.running"> · 运行中 {{ subCounts.running }}</span>
      <span v-if="subCounts.waiting"> · 等待 {{ subCounts.waiting }}</span>
      {{ running ? ' · 状态每 2 秒自动刷新' : '' }} ·
      点击任务行可查看详情、实时日志与产物
    </p>
    <div class="filters">
      <select v-model="filters.state">
        <option value="">全部状态</option>
        <option v-for="s in stateOptions" :key="s" :value="s">{{ STATE_TEXT[s] || s }}</option>
      </select>
      <select v-model="filters.kind">
        <option value="">全部失败原因</option>
        <option v-for="k in kindOptions" :key="k" :value="k">{{ KIND_TEXT[k] || k }}</option>
      </select>
      <input v-model.trim="filters.q" type="text" placeholder="搜索任务名 / 说明 / 异常" />
      <select v-model.number="filters.limit">
        <option :value="100">显示 100 条</option>
        <option :value="200">显示 200 条</option>
        <option :value="500">显示 500 条</option>
      </select>
      <button class="btn sm" @click="loadSubtasks">刷新</button>
    </div>

    <div v-if="!subtasks.length" class="empty">暂无子任务</div>
    <div v-else class="table-wrap tall fixed">
      <table>
        <colgroup>
          <col style="width: 60px" />
          <col style="width: 210px" />
          <col style="width: 250px" />
          <col style="width: 110px" />
          <col style="width: 100px" />
          <col style="width: 80px" />
          <col style="width: 70px" />
          <col style="width: 120px" />
          <col style="width: 90px" />
        </colgroup>
        <thead>
          <tr>
            <th class="num">序号</th>
            <th>任务</th>
            <th>标题</th>
            <th class="center">状态</th>
            <th class="center">失败原因</th>
            <th class="num">reward</th>
            <th class="num">步数</th>
            <th class="num">token(入/出)</th>
            <th class="num">耗时s</th>
          </tr>
        </thead>
        <tbody>
          <template v-for="row in visibleTasks" :key="row.task">
            <tr class="clickable" :class="rowClass(row)" @click="toggleTask(row)">
              <td class="num">{{ row.index }}</td>
              <td class="mono clip" :title="row.trial || row.task">{{ row.task }}</td>
              <td><span class="clip" :title="row.title">{{ row.title }}</span></td>
              <td class="center">
                <span class="badge" :class="badgeClass(row)">{{ row.state_text }}</span>
                <div v-if="row.result_text" class="muted">{{ row.result_text }}</div>
                <div v-else-if="row.state === 'running'" class="muted">
                  已跑 {{ row.elapsed_s === null || row.elapsed_s === undefined ? '—' : duration(row.elapsed_s) }}
                </div>
              </td>
              <td class="center">
                <span v-if="row.state === 'error' && row.failure_kind_text"
                      class="badge bad" :title="row.error">{{ row.failure_kind_text }}</span>
                <span v-else class="muted">—</span>
              </td>
              <td class="num">{{ row.reward === null || row.reward === undefined ? '—' : shortFixed(row.reward, 3) }}</td>
              <td class="num">{{ row.steps === null || row.steps === undefined ? '—' : int(row.steps) }}</td>
              <td class="num">{{ compact(row.input_tokens) }} / {{ compact(row.output_tokens) }}</td>
              <td class="num">{{ row.elapsed_s === null || row.elapsed_s === undefined ? '—' : shortFixed(row.elapsed_s) }}</td>
            </tr>
            <tr v-if="openTask === row.task" class="detail-row">
              <td colspan="9">
                <KvList :items="detailRows(row)" />
                <div class="row mt">
                  <a v-if="row.trial_dir" class="btn sm" :href="artifactUrl(row, 'verifier/test-stdout.txt')" target="_blank">测试输出</a>
                  <a v-if="row.has_patch" class="btn sm" :href="artifactUrl(row, 'artifacts/model.patch')" target="_blank">补丁</a>
                  <a v-if="row.trial_dir" class="btn sm" :href="artifactUrl(row, 'trial.log')" target="_blank">trial.log</a>
                  <a v-if="row.trial_dir" class="btn sm" :href="artifactUrl(row, 'instruction.md')" target="_blank">任务说明</a>
                </div>
                <div class="mt">
                  <div class="muted">
                    实时日志 <span class="mono">{{ row.log_rel || '（尚未产生）' }}</span>
                    <span v-if="row.log_size"> · {{ compact(row.log_size) }} B</span>
                  </div>
                  <LogPanel :logs="taskLogs" />
                </div>
              </td>
            </tr>
          </template>
        </tbody>
      </table>
    </div>
  </Card>

  <Card v-if="Object.keys(summary).length" title="汇总数据">
    <div class="stats">
      <StatTile
        v-for="[k, v] in groupRows(summary.by_category)"
        :key="k"
        :label="`${k} (通过率)`"
        :value="pct(v.pass_rate)"
        :sub="`${v.resolved}/${v.total}`"
      />
    </div>
    <div v-if="kindRows.some((r) => r.count)" class="stats mt">
      <StatTile
        v-for="r in kindRows.filter((x) => x.count)"
        :key="r.kind"
        :label="r.text"
        :value="int(r.count)"
        :tone="r.kind === 'timeout' || r.kind === 'environment' ? 'bad' : ''"
      />
    </div>
    <p class="hint mt">
      上排按任务类别统计；下排为失败原因分布（超时 / 指令 / 环境 / 判分未通过）。
      环境异常与超时失败反映评测环境与预算，不计入通过率分母，但仍逐条记录；
      详细字段与结果 JSON 一致
    </p>
    <KvList :items="Object.entries(summary).map(([k, v]) => ({ k, v: typeof v === 'object' ? JSON.stringify(v) : String(v) }))" />
  </Card>

  <Card title="运行日志">
    <LogPanel :logs="(snap && snap.logs) || []" />
  </Card>
</template>
