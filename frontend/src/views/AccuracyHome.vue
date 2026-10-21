<script setup>
import { computed, onMounted, onUnmounted, reactive, ref } from 'vue'
import { RouterLink, useRouter } from 'vue-router'
import { api } from '../api'
import { store } from '../store'
import { ProgressBar, StatusBadge } from '../components/ui'
import { Card } from '../components/index.js'
import { duration, int, isActive, isRunning, pct } from '../format'

const router = useRouter()
const form = reactive({})
const runs = ref([])
const queueInfo = ref(null)
const error = ref('')
const submitting = ref(false)
const refreshing = ref(false)
const busyId = ref('')
let timer = null

const datasets = computed(() => (store.meta && store.meta.accuracy.datasets) || [])
const datasetDir = computed(() => (store.meta && store.meta.accuracy.dataset_dir) || '')
// 下拉框选中的数据集条目：用于展示说明/缺失提示，并决定能否预览 CSV
const currentDataset = computed(
  () => datasets.value.find((d) => d.path === form.data) || null,
)
const previewPath = computed(() => form.data_custom || form.data || '')
const canPreviewCsv = computed(() => {
  if (form.data_custom) return form.data_custom.toLowerCase().endsWith('.csv')
  return !!(currentDataset.value && currentDataset.value.reader === 'csv')
})
const thinkingOptions = computed(() => (store.meta && store.meta.accuracy.thinking_options) || [])
const effortOptions = computed(() => (store.meta && store.meta.accuracy.effort_options) || [])
const savedAt = computed(() => {
  const t = store.meta && store.meta.accuracy.saved_cfg_at
  return t ? new Date(t * 1000).toLocaleString() : ''
})
// 排队中的任务也在等状态变化，列表同样需要自动刷新
const hasActive = computed(() => runs.value.some((r) => isActive(r.status)))

// 全局队列概览：精度与压测共用一条串行队列，避免并行测试互相抢资源
const queueText = computed(() => {
  const q = queueInfo.value
  if (!q) return ''
  const parts = []
  if (q.running) parts.push(`${q.running.kind_text} ${q.running.run_id} 运行中`)
  const count = q.pending_count || 0
  if (count) {
    const next = (q.pending || [])[0] || {}
    parts.push(`排队 ${count} 个${next.run_id ? `（下一个 ${next.kind_text} ${next.run_id}）` : ''}`)
  }
  return parts.join(' · ')
})

const applyDefaults = () => {
  const d = store.accuracyDefaults
  Object.assign(form, {
    endpoint: d.endpoint || '',
    api_key: d.api_key || '',
    model: d.model || '',
    judge_model: d.judge_model || '',
    judge_endpoint: d.judge_endpoint || '',
    judge_api_key: d.judge_api_key || '',
    data: d.data || '',
    data_custom: (store.meta && store.meta.accuracy.custom_data) || '',
    thinking: d.thinking || '',
    reasoning_effort: d.reasoning_effort || '',
    judge_thinking: d.judge_thinking || '',
    judge_reasoning_effort: d.judge_reasoning_effort || '',
    concurrency: d.concurrency ?? 8,
    judge_concurrency: d.judge_concurrency ?? 8,
    limit: d.limit ?? 0,
    seed: d.seed ?? 0,
    max_tokens: d.max_tokens ?? 32768,
    max_tokens_cap: d.max_tokens_cap ?? 65536,
    max_incomplete_attempts: d.max_incomplete_attempts ?? 2,
  })
}

const loadRuns = async () => {
  try {
    const data = await api.accuracyRuns()
    runs.value = data.runs || []
  } catch (e) {
    error.value = e.message
  }
  try {
    queueInfo.value = await api.queue()
  } catch (e) {
    queueInfo.value = null              // 队列概览只是辅助信息，拉取失败不影响列表
  }
}

const loadAll = async (force = false) => {
  refreshing.value = true
  await store.load(force)
  applyDefaults()
  await loadRuns()
  refreshing.value = false
}

const submit = async () => {
  error.value = ''
  submitting.value = true
  try {
    // 自定义路径优先于内置数据集选择
    const { run_id: runId } = await api.accuracyStart({
      ...form,
      data: form.data_custom || form.data,
    })
    // 列表先落地；导航被中止（旧构建的按需加载 chunk 失效）时整页跳转兜底
    loadRuns()
    await router.push(`/accuracy/${runId}`).catch(() => {})
    if (router.currentRoute.value.path !== `/accuracy/${runId}`) window.location.assign(`/accuracy/${runId}`)
  } catch (e) {
    error.value = e.message
  } finally {
    submitting.value = false
  }
}

const stopRun = async (run) => {
  if (!window.confirm(`确认停止任务 ${run.run_id}？已完成的部分结果会保留。`)) return
  busyId.value = run.run_id
  try {
    await api.accuracyStop(run.run_id)
    await loadRuns()
  } catch (e) {
    error.value = e.message
  } finally {
    busyId.value = ''
  }
}

const deleteRun = async (run) => {
  if (!window.confirm(`确认删除任务 ${run.run_id}？该任务的输出文件会一并删除，且不可恢复。`)) return
  busyId.value = run.run_id
  try {
    await api.accuracyDelete(run.run_id)
    await loadRuns()
  } catch (e) {
    error.value = e.message
  } finally {
    busyId.value = ''
  }
}

onMounted(async () => {
  // force=true：提交任务后返回本页时 store 中仍是旧 meta，
  // 不强制刷新会让表单沿用上一次的配置（与压测页同一口径）
  await loadAll(true)
  timer = setInterval(() => {
    if (hasActive.value) loadRuns()
  }, 5000)
})
onUnmounted(() => timer && clearInterval(timer))
</script>

<template>
  <div class="page-head">
    <div>
      <h1>精度测试</h1>
      <div class="sub">数据集逐题作答 + 裁判模型判分，统计 pass@1（数据集由 dataset/datasets.json 配置）</div>
    </div>
    <div class="actions">
      <button class="btn" :disabled="refreshing" @click="loadAll(true)">刷新配置</button>
    </div>
  </div>

  <div v-if="error" class="alert bad">{{ error }}</div>
  <div v-if="store.meta && !store.meta.accuracy.has_data" class="alert warn">
    没有找到可用的数据集：请确认 dataset/datasets.json 里已登记数据集，且数据文件真实存在。
  </div>

  <section class="card">
    <Card title="新建评测任务" nested>
    <p class="hint">
      裁判端点 / 密钥留空时复用被测模型的配置
      <span v-if="savedAt"> · 表单已沿用上次提交的配置（{{ savedAt }}）</span>
    </p>

    <div class="grid">
      <div class="field span-2">
        <label>被测模型 API 端点</label>
        <input v-model.trim="form.endpoint" type="text" placeholder="http://host:port/v1/chat/completions" />
      </div>
      <div class="field">
        <label>被测模型 API Key</label>
        <input v-model.trim="form.api_key" type="password" autocomplete="off" />
      </div>
      <div class="field">
        <label>被测模型名</label>
        <input v-model.trim="form.model" type="text" />
      </div>
      <div class="field span-2">
        <label>裁判模型 API 端点（留空 = 复用被测端点）</label>
        <input v-model.trim="form.judge_endpoint" type="text" placeholder="留空复用被测端点" />
      </div>
      <div class="field">
        <label>裁判模型 API Key（留空 = 复用）</label>
        <input v-model.trim="form.judge_api_key" type="password" autocomplete="off" />
      </div>
      <div class="field">
        <label>裁判模型名</label>
        <input v-model.trim="form.judge_model" type="text" />
      </div>
    </div>

    </Card>
    <Card title="数据集" nested>
    <div class="grid wide">
      <div class="field">
        <label>数据集</label>
        <select v-model="form.data">
          <option
            v-for="d in datasets"
            :key="d.path"
            :value="d.path"
            :disabled="!d.runnable"
          >
            {{ d.title }}{{ d.exists ? '' : '（数据文件缺失）' }}{{ d.runnable ? '' : '（暂不支持本流程）' }}
          </option>
        </select>
        <span v-if="!datasets.length" class="tip">
          还没有配置数据集：把数据集目录放进 {{ datasetDir || 'dataset/' }}（目录名即数据集名称），
          再在 dataset/datasets.json 里登记一条即可
        </span>
        <span v-else-if="currentDataset && !currentDataset.exists" class="tip">
          数据文件不存在，请检查 {{ currentDataset.dir }}
        </span>
        <span v-else-if="currentDataset && currentDataset.note" class="tip">
          {{ currentDataset.note }}
        </span>
        <span v-else class="tip">
          数据集以名称作为顶级目录放在 {{ datasetDir || 'dataset/' }}，清单与字段映射见
          dataset/datasets.json，新增数据集无需改代码
        </span>
      </div>
      <div class="field">
        <label>自定义数据集路径（优先于上面的选择）</label>
        <input v-model.trim="form.data_custom" type="text" placeholder="留空则用上面选择的数据集" />
        <span class="tip">
          支持工作区内的 .csv / parquet 路径（未在配置里登记也能直接跑）·
          <RouterLink v-if="canPreviewCsv" :to="`/csv?path=${encodeURIComponent(previewPath)}`">
            预览 CSV
          </RouterLink>
        </span>
      </div>
    </div>

    </Card>
    <Card title="生成与判分参数" nested>
    <div class="grid">
      <div class="field">
        <label>思考模式</label>
        <select v-model="form.thinking">
          <option v-for="[v, label] in thinkingOptions" :key="v" :value="v">{{ label }}</option>
        </select>
      </div>
      <div class="field">
        <label>思考强度</label>
        <select v-model="form.reasoning_effort">
          <option v-for="[v, label] in effortOptions" :key="v" :value="v">{{ label }}</option>
        </select>
      </div>
      <div class="field">
        <label>裁判思考模式</label>
        <select v-model="form.judge_thinking">
          <option v-for="[v, label] in thinkingOptions" :key="v" :value="v">{{ label }}</option>
        </select>
      </div>
      <div class="field">
        <label>裁判思考强度</label>
        <select v-model="form.judge_reasoning_effort">
          <option v-for="[v, label] in effortOptions" :key="v" :value="v">{{ label }}</option>
        </select>
      </div>
      <div class="field">
        <label>作答并发</label>
        <input v-model.number="form.concurrency" type="number" min="1" max="256" />
      </div>
      <div class="field">
        <label>判分并发</label>
        <input v-model.number="form.judge_concurrency" type="number" min="1" max="256" />
      </div>
      <div class="field">
        <label>题数上限（0 = 全部）</label>
        <input v-model.number="form.limit" type="number" min="0" />
      </div>
      <div class="field">
        <label>随机种子（0 = 不固定）</label>
        <input v-model.number="form.seed" type="number" min="0" />
      </div>
      <div class="field">
        <label>max_tokens</label>
        <input v-model.number="form.max_tokens" type="number" min="1" />
      </div>
      <div class="field">
        <label>max_tokens 上限</label>
        <input v-model.number="form.max_tokens_cap" type="number" min="1" />
      </div>
      <div class="field">
        <label>未完成重试次数</label>
        <input v-model.number="form.max_incomplete_attempts" type="number" min="1" />
      </div>
    </div>

    </Card>
    <div class="row mt">
      <button class="btn primary" :disabled="submitting" @click="submit">
        {{ submitting ? '正在启动…' : '启动评测' }}
      </button>
      <span class="muted">启动后进入任务详情页，可实时查看进度与逐题结果。</span>
    </div>
  </section>

  <Card title="历史任务">
    <p class="hint">共 {{ runs.length }} 个任务{{ hasActive ? ' · 有任务正在运行或排队，列表每 5 秒自动刷新' : '' }}</p>
    <p v-if="queueText" class="hint">全局测试队列：{{ queueText }}（精度与压测共用，严格串行、逐个执行）</p>
    <div v-if="!runs.length" class="empty">暂无评测任务</div>
    <div v-else class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>任务</th>
            <th>状态</th>
            <th>数据集</th>
            <th>模型 / 裁判</th>
            <th>进度</th>
            <th class="num">pass@1</th>
            <th class="num">完成题数</th>
            <th class="num">耗时</th>
            <th style="width: 210px">操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="run in runs" :key="run.run_id">
            <td>
              <RouterLink :to="`/accuracy/${run.run_id}`" class="mono">{{ run.run_id }}</RouterLink>
              <div class="muted">{{ run.started_at_str }}</div>
            </td>
            <td><StatusBadge :status="run.status" :text="run.status_text" /></td>
            <td class="mono clip" :title="run.cfg && run.cfg.data">
              {{ (run.cfg && run.cfg.data) || '—' }}
            </td>
            <td>
              {{ (run.cfg && run.cfg.model) || '—' }}
              <div class="muted">{{ (run.cfg && run.cfg.judge_model) || '—' }}</div>
            </td>
            <td style="min-width: 170px">
              <div class="muted">{{ run.stage }}</div>
              <ProgressBar :pct="run.pct" :status="run.status" :text="`${(run.pct || 0).toFixed(0)}%`" />
            </td>
            <td class="num">{{ run.pass_at_1 === null || run.pass_at_1 === undefined ? '—' : pct(run.pass_at_1) }}</td>
            <td class="num">{{ int(run.n_done) }} / {{ int(run.expected) }}</td>
            <td class="num">{{ duration(run.duration_s) }}</td>
            <td>
              <div class="row">
                <RouterLink class="btn sm" :to="`/accuracy/${run.run_id}`">详情</RouterLink>
                <button
                  v-if="isActive(run.status)"
                  class="btn sm danger"
                  :disabled="busyId === run.run_id"
                  @click="stopRun(run)"
                >
                  {{ isRunning(run.status) ? '停止' : '取消排队' }}
                </button>
                <button
                  class="btn sm danger"
                  :disabled="busyId === run.run_id"
                  @click="deleteRun(run)"
                >
                  删除
                </button>
              </div>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </Card>
</template>
