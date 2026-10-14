<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'
import { api } from '../api'
import { KvList, LogPanel, ProgressBar, StatTile, StatusBadge } from '../components/index.js'
import { compact, duration, int, isRunning, num, pct, shortFixed } from '../format'

const route = useRoute()
const router = useRouter()
const runId = route.params.id

const snap = ref(null)
const items = ref([])
const itemTotal = ref(0)
const error = ref('')
const busy = ref(false)
const filters = ref({ grade: '', q: '', limit: 200 })
const t0 = ref(Date.now())
let timer = null
let tick = 0

const running = computed(() => isRunning(snap.value && snap.value.status))
const gen = computed(() => (snap.value && snap.value.gen) || {})
const judge = computed(() => (snap.value && snap.value.judge) || {})
const summary = computed(() => (snap.value && snap.value.summary) || {})
const grades = computed(() => [...new Set(items.value.map((i) => i.grade).filter(Boolean))])

// 判定取值来自后端 GRADE_*（CORRECT / INCORRECT / NOT_ATTEMPTED…），统一小写比较
const gradeOf = (g) => String(g || '').trim().toLowerCase()
const rowClass = (row) => {
  const g = gradeOf(row.grade)
  if (g === 'correct') return 'row-ok'
  if (g === 'incorrect') return 'row-bad'
  if (g === 'not_attempted') return 'row-warn'
  return ''
}

const summaryRows = computed(() =>
  Object.entries(summary.value).map(([k, v]) => [
    k,
    typeof v === 'object' ? JSON.stringify(v, null, 0) : String(v),
  ]),
)

const loadItems = async () => {
  try {
    const data = await api.accuracyItems(runId, filters.value)
    items.value = data.items || []
    itemTotal.value = data.total || 0
  } catch (e) {
    error.value = e.message
  }
}

const loadSnap = async () => {
  try {
    snap.value = await api.accuracyRun(runId)
    error.value = ''
  } catch (e) {
    error.value = e.message
  }
}

const tickLoop = async () => {
  tick += 1
  await loadSnap()
  if (tick % 5 === 0 || !running.value) await loadItems()
}

const stopRun = async () => {
  if (!window.confirm('确认停止该评测任务？已完成的部分结果会保留。')) return
  busy.value = true
  try {
    await api.accuracyStop(runId)
    await loadSnap()
  } catch (e) {
    error.value = e.message
  } finally {
    busy.value = false
  }
}

const deleteRun = async () => {
  if (!window.confirm(`确认删除任务 ${runId}？输出文件会一并删除，且不可恢复。`)) return
  busy.value = true
  try {
    await api.accuracyDelete(runId)
    router.push('/accuracy')
  } catch (e) {
    error.value = e.message
    busy.value = false
  }
}

onMounted(async () => {
  await loadSnap()
  await loadItems()
  timer = setInterval(() => {
    if (running.value) tickLoop()
  }, 2000)
})
onUnmounted(() => timer && clearInterval(timer))
</script>

<template>
  <div class="page-head">
    <div>
      <h1 class="row">
        评测任务 <span class="mono">{{ runId }}</span>
        <StatusBadge v-if="snap" :status="snap.status" :text="snap.status_text" />
      </h1>
      <div class="sub" v-if="snap">
        {{ snap.cfg.model }} · 裁判 {{ snap.cfg.judge_model }} · 数据集 {{ snap.cfg.data }}
      </div>
    </div>
    <div class="actions">
      <RouterLink class="btn" to="/accuracy">返回列表</RouterLink>
      <RouterLink class="btn" :to="`/accuracy/${runId}/csv`">结果表格</RouterLink>
      <a class="btn" :href="`/api/accuracy/runs/${runId}/download.csv`">下载 CSV</a>
      <button v-if="running" class="btn danger" :disabled="busy" @click="stopRun">停止</button>
      <button class="btn danger" :disabled="busy" @click="deleteRun">删除</button>
    </div>
  </div>

  <div v-if="error" class="alert bad">{{ error }}</div>
  <div v-if="snap && snap.error" class="alert bad">失败原因：{{ snap.error }}</div>
  <div v-if="snap && snap.summary && snap.summary._stopped" class="alert warn">
    该任务在完成前被停止，汇总仅覆盖已完成的部分。
  </div>

  <section v-if="snap" class="card">
    <h2>运行进度</h2>
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
        label="pass@1"
        :value="summary.pass_at_1 === undefined || summary.pass_at_1 === null ? '—' : pct(summary.pass_at_1)"
        :tone="summary.pass_at_1 >= 0.5 ? 'good' : ''"
      />
      <StatTile label="完成题数" :value="int(summary.num_questions)" :sub="`/ ${int(summary.expected_questions)}`" />
      <StatTile label="生成失败" :value="int(gen.errors)" :tone="gen.errors ? 'bad' : ''" />
      <StatTile label="判分失败" :value="int(judge.errors)" :tone="judge.errors ? 'bad' : ''" />
      <StatTile label="单请求 TPS" :value="num(gen.tps, 1)" sub="tokens/s" />
      <StatTile label="输出 token" :value="compact(gen.tokens)" />
      <StatTile label="数据集" :value="int((snap.load || {}).total)" sub="题" />
      <StatTile label="耗时" :value="duration(snap.duration_s)" />
    </div>
  </section>

  <section class="card">
    <h2>逐题结果</h2>
    <p class="hint">
      共 {{ itemTotal }} 条{{ running ? '（任务运行中，每 10 秒自动刷新）' : '' }}
    </p>
    <div class="filters">
      <select v-model="filters.grade" @change="loadItems">
        <option value="">全部判定</option>
        <option v-for="g in grades" :key="g" :value="g">{{ g }}</option>
      </select>
      <input v-model.trim="filters.q" type="text" placeholder="搜索问题或模型回答" @keyup.enter="loadItems" />
      <select v-model.number="filters.limit" @change="loadItems">
        <option :value="100">显示 100 条</option>
        <option :value="200">显示 200 条</option>
        <option :value="500">显示 500 条</option>
        <option :value="2000">显示 2000 条</option>
      </select>
      <button class="btn sm" @click="loadItems">查询</button>
    </div>

    <div v-if="!items.length" class="empty">暂无逐题结果</div>
    <div v-else class="table-wrap tall fixed">
      <table>
        <colgroup>
          <col style="width: 76px" />
          <col style="width: 96px" />
          <col style="width: 320px" />
          <col style="width: 360px" />
          <col style="width: 180px" />
          <col style="width: 260px" />
          <col style="width: 120px" />
          <col style="width: 130px" />
          <col style="width: 90px" />
          <col style="width: 100px" />
          <col style="width: 110px" />
        </colgroup>
        <thead>
          <tr>
            <th class="num">题号</th>
            <th class="center">判定</th>
            <th>问题</th>
            <th>模型回答</th>
            <th>抽取答案</th>
            <th>标准答案</th>
            <th>主题</th>
            <th class="num">token(总/思考)</th>
            <th class="num">首字s</th>
            <th class="num">总耗时s</th>
            <th class="center">结束原因</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(row, i) in items" :key="i" :class="rowClass(row)">
            <td class="num">{{ row.original_index }}</td>
            <td class="center">
              <span class="badge" :class="{ ok: gradeOf(row.grade) === 'correct', bad: gradeOf(row.grade) === 'incorrect', warn: gradeOf(row.grade) === 'not_attempted' || row.incomplete }">
                {{ row.grade || '—' }}
              </span>
            </td>
            <td><span class="clip" :title="row.problem">{{ row.problem }}</span></td>
            <td><span class="clip" :title="row.prediction">{{ row.prediction }}</span></td>
            <td><span class="clip" :title="row.exact_answer">{{ row.exact_answer || '—' }}</span></td>
            <td><span class="clip" :title="row.answer">{{ row.answer }}</span></td>
            <td>{{ row.topic || '—' }}</td>
            <td class="num">
              {{ int(row.completion_tokens) }} / {{ int(row.reasoning_tokens) }}
            </td>
            <td class="num">{{ shortFixed(row.ttft_s) }}</td>
            <td class="num">{{ shortFixed(row.total_s) }}</td>
            <td class="center">{{ row.finish_reason || '—' }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </section>

  <section v-if="summaryRows.length" class="card">
    <h2>汇总数据</h2>
    <p class="hint">summarise() 输出的原始字段，与结果 JSON 一致</p>
    <KvList :items="summaryRows.map(([k, v]) => ({ k, v }))" />
  </section>

  <section class="card">
    <h2>运行日志</h2>
    <LogPanel :logs="(snap && snap.logs) || []" />
  </section>
</template>
