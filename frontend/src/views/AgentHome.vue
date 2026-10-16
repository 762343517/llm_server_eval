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
const env = ref({})
const tasks = ref([])
const taskTotal = ref(0)
const showTasks = ref(false)
const error = ref('')
const notice = ref('')
const submitting = ref(false)
const saving = ref(false)
const refreshing = ref(false)
const busyId = ref('')
let timer = null

const datasets = computed(() => store.agentDatasets)
const choices = computed(() => store.agentChoices)
const savedAt = computed(() => {
  const t = store.meta && store.meta.agent.saved_cfg_at
  return t ? new Date(t * 1000).toLocaleString() : ''
})
const hasActive = computed(() => runs.value.some((r) => isActive(r.status)))
// 环境不可用时后端会直接拒绝启动，这里提前给出提示
const envReady = computed(() => !!env.value.ok)
const modelFree = computed(() => ['oracle', 'nop'].includes(form.agent))
const selectedDataset = computed(
  () => datasets.value.find((d) => (d.agent || {}).tasks_dir === form.data) || null,
)
const imageReady = computed(() => tasks.value.filter((t) => t.image_ready).length)

// agent 说明：mini-swe-agent 是被测对象，oracle/nop 用于自检评测链路
const AGENT_TEXT = {
  'mini-swe-agent': 'mini-swe-agent：让被测模型在容器内多轮改代码',
  oracle: 'oracle：直接应用任务自带参考解，用来验证镜像与判定链路',
  nop: 'nop：不做任何修改，用来验证初始化与测试链路',
}

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
  const d = store.agentDefaults
  // 上次保存的任务集可能来自「自定义任务集路径」（不在下拉候选里），此时回填到自定义框，
  // 否则下拉框会显示空白
  const known = datasets.value.map((x) => (x.agent || {}).tasks_dir)
  const custom = d.data && !known.includes(d.data) ? d.data : ''
  Object.assign(form, {
    agent: d.agent || 'mini-swe-agent',
    endpoint: d.endpoint || '',
    api_key: d.api_key || '',
    model: d.model || '',
    model_class: d.model_class || 'litellm',
    data: custom ? known[0] || '' : d.data || '',
    data_custom: custom,
    tasks: '',
    limit: d.limit ?? 1,
    seed: d.seed ?? 0,
    concurrency: d.concurrency ?? 2,
    timeout_multiplier: d.timeout_multiplier ?? 1,
    verifier_timeout_multiplier: d.verifier_timeout_multiplier ?? 1,
  })
}

const loadEnv = async () => {
  try {
    env.value = await api.agentEnv()
  } catch (e) {
    error.value = e.message
  }
}

const loadTasks = async () => {
  try {
    const data = await api.agentTasks(form.data_custom || form.data)
    tasks.value = data.tasks || []
    taskTotal.value = tasks.value.length
  } catch (e) {
    tasks.value = []
    taskTotal.value = 0
    error.value = e.message
  }
}

const loadRuns = async () => {
  try {
    const data = await api.agentRuns()
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
  error.value = ''
  await store.load(force)
  applyDefaults()
  await Promise.all([loadEnv(), loadTasks(), loadRuns()])
  refreshing.value = false
}

// 手动保存：只校验并落盘，不启动任务（改了表单没启动 / 启动被拒时配置不会丢）
const saveConfig = async () => {
  error.value = ''
  notice.value = ''
  saving.value = true
  try {
    await api.agentSaveConfig({ ...form, data: form.data_custom || form.data })
    // 只刷新 meta 取新的 saved_cfg_at，不再 applyDefaults，避免覆盖当前表单
    await store.load(true)
    notice.value = `配置已保存（${savedAt.value}），下次打开本页会沿用`
  } catch (e) {
    error.value = e.message
  } finally {
    saving.value = false
  }
}

const submit = async () => {
  error.value = ''
  notice.value = ''
  submitting.value = true
  try {
    const { run_id: runId } = await api.agentStart({
      ...form,
      data: form.data_custom || form.data,
    })
    router.push(`/agent/${runId}`)
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
    await api.agentStop(run.run_id)
    await loadRuns()
  } catch (e) {
    error.value = e.message
  } finally {
    busyId.value = ''
  }
}

const deleteRun = async (run) => {
  if (!window.confirm(`确认删除任务 ${run.run_id}？容器产物与结果文件会一并删除，且不可恢复。`)) return
  busyId.value = run.run_id
  try {
    await api.agentDelete(run.run_id)
    await loadRuns()
  } catch (e) {
    error.value = e.message
  } finally {
    busyId.value = ''
  }
}

onMounted(async () => {
  // force=true：提交任务后返回本页时 store 中仍是旧 meta，需要重新拉取默认值
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
      <h1>Agent 评测</h1>
      <div class="sub">
        逐任务起 docker 沙箱，让 agent 在容器内改代码，再由任务自带测试判定 reward（pier 驱动，任务集由 dataset/datasets.json 配置）
      </div>
    </div>
    <div class="actions">
      <button class="btn" :disabled="refreshing" @click="loadAll(true)">刷新配置与自检</button>
    </div>
  </div>

  <div v-if="error" class="alert bad">{{ error }}</div>
  <div v-if="notice" class="alert info">{{ notice }}</div>
  <div v-if="!envReady" class="alert warn">
    Agent 评测环境尚未就绪：{{ (env.notes || []).join('；') || '缺少 docker 或 pier' }}
  </div>
  <div v-else class="alert info">
    环境就绪：{{ (env.notes || []).join('；') }}
  </div>
  <div v-if="!datasets.length" class="alert warn">
    还没有可用的 Agent 任务集：把带 task.toml 的任务目录放进 dataset/ 下，再在
    dataset/datasets.json 里登记 agent 段（framework / tasks）即可。
  </div>

  <section class="card">
    <Card title="新建评测任务" nested>
      <p class="hint">
        Agent 评测与精度/压测共用同一条串行测试队列，避免并行占用机器资源
        <span v-if="savedAt"> · 表单已沿用上次保存的配置（{{ savedAt }}），改动后可点「保存配置」留作默认值</span>
      </p>

      <div class="grid">
        <div class="field">
          <label>执行 agent</label>
          <select v-model="form.agent">
            <option v-for="a in choices" :key="a" :value="a">{{ a }}</option>
          </select>
          <span class="tip">{{ AGENT_TEXT[form.agent] || '' }}</span>
        </div>
        <div class="field">
          <label>模型调用方式（litellm 走 OpenAI 兼容接口）</label>
          <input v-model.trim="form.model_class" type="text" placeholder="litellm" />
        </div>
        <div class="field span-2">
          <label>被测模型 API 端点</label>
          <input v-model.trim="form.endpoint" type="text" placeholder="http://host:port/v1/chat/completions" />
          <span class="tip">填到 chat/completions 也可，后端会自动截成服务根地址传给 agent</span>
        </div>
        <div class="field">
          <label>被测模型 API Key</label>
          <input v-model.trim="form.api_key" type="password" autocomplete="off" />
        </div>
        <div class="field">
          <label>被测模型名</label>
          <input v-model.trim="form.model" type="text" :disabled="modelFree" />
          <span v-if="modelFree" class="tip">oracle / nop 不调用模型，可忽略这三项</span>
        </div>
      </div>
    </Card>

    <Card title="任务集" nested>
      <div class="grid wide">
        <div class="field">
          <label>任务集</label>
          <select v-model="form.data" @change="loadTasks">
            <option v-for="d in datasets" :key="d.name" :value="(d.agent || {}).tasks_dir">
              {{ d.title }}（{{ (d.agent || {}).tasks_text || '—' }}）
            </option>
          </select>
          <span v-if="selectedDataset && selectedDataset.note" class="tip">
            {{ selectedDataset.note }}
          </span>
          <span class="tip">
            已解析 {{ taskTotal }} 个任务，其中 {{ imageReady }} 个镜像已在本机
          </span>
        </div>
        <div class="field">
          <label>自定义任务集路径（优先于上面的选择）</label>
          <input v-model.trim="form.data_custom" type="text" placeholder="留空则用上面选择的任务集" />
          <span class="tip">
            支持任意含 <code>task.toml</code> 子目录的路径（未登记的数据集也能直接跑）·
            <a href="#" @click.prevent="loadTasks">重新加载任务清单</a>
          </span>
        </div>
      </div>
      <div class="row mt">
        <button class="btn sm" @click="showTasks = !showTasks">
          {{ showTasks ? '收起任务清单' : '查看任务清单' }}
        </button>
      </div>
      <div v-if="showTasks" class="table-wrap mt">
        <table v-if="tasks.length">
          <thead>
            <tr>
              <th>任务</th>
              <th>标题</th>
              <th>类别</th>
              <th>语言</th>
              <th class="center">镜像</th>
              <th class="num">agent超时(s)</th>
              <th class="num">判定超时(s)</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="t in tasks" :key="t.name">
              <td class="mono clip" :title="t.path">{{ t.name }}</td>
              <td><span class="clip" :title="t.title">{{ t.title }}</span></td>
              <td>{{ t.category || '—' }}</td>
              <td>{{ t.language || '—' }}</td>
              <td class="center">
                <span class="badge" :class="t.image_ready ? 'ok' : 'warn'">
                  {{ t.image_ready ? '已就绪' : '需拉取' }}
                </span>
              </td>
              <td class="num">{{ t.agent_timeout_sec || '—' }}</td>
              <td class="num">{{ t.verifier_timeout_sec || '—' }}</td>
            </tr>
          </tbody>
        </table>
        <div v-else class="empty">暂无任务（请检查任务集路径）</div>
      </div>
    </Card>

    <Card title="运行参数" nested>
      <div class="grid">
        <div class="field">
          <label>任务数（0 = 全部）</label>
          <input v-model.number="form.limit" type="number" min="0" />
        </div>
        <div class="field">
          <label>任务抽样种子（0 = 按目录顺序取前 N 个）</label>
          <input v-model.number="form.seed" type="number" min="0" />
        </div>
        <div class="field">
          <label>并发任务数（每个任务一个容器）</label>
          <input v-model.number="form.concurrency" type="number" min="1" max="32" />
        </div>
        <div class="field">
          <label>任务超时倍数</label>
          <input v-model.number="form.timeout_multiplier" type="number" min="0.01" step="0.01" />
          <span class="tip">
            1 = 用任务自带超时（DeepSWE 单个任务通常 1~2 小时）；小于 1 可用于快速冒烟
          </span>
        </div>
        <div class="field">
          <label>判定超时倍数</label>
          <input v-model.number="form.verifier_timeout_multiplier" type="number" min="0.01" step="0.01" />
          <span class="tip">测试阶段超时倍数，通常保持 1 即可</span>
        </div>
      </div>
    </Card>

    <div class="row mt">
      <button class="btn primary" :disabled="submitting || !envReady" @click="submit">
        {{ submitting ? '正在启动…' : '启动 Agent 评测' }}
      </button>
      <button class="btn" :disabled="saving" @click="saveConfig">
        {{ saving ? '保存中…' : '保存配置' }}
      </button>
      <span class="muted">
        启动后进入任务详情页；镜像首次拉取需要等待，agent 每步耗时都较长，请耐心观察日志。
      </span>
    </div>
  </section>

  <Card title="历史任务">
    <p class="hint">共 {{ runs.length }} 个任务{{ hasActive ? ' · 有任务正在运行或排队，列表每 5 秒自动刷新' : '' }}</p>
    <p v-if="queueText" class="hint">全局测试队列：{{ queueText }}（精度/压测/Agent 共用，严格串行、逐个执行）</p>
    <div v-if="!runs.length" class="empty">暂无 Agent 评测任务</div>
    <div v-else class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>任务</th>
            <th>状态</th>
            <th>任务集</th>
            <th>agent / 模型</th>
            <th>进度</th>
            <th class="num">通过率</th>
            <th class="num">完成 / 计划</th>
            <th class="num">耗时</th>
            <th style="width: 210px">操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="run in runs" :key="run.run_id">
            <td>
              <RouterLink :to="`/agent/${run.run_id}`" class="mono">{{ run.run_id }}</RouterLink>
              <div class="muted">{{ run.started_at_str }}</div>
            </td>
            <td><StatusBadge :status="run.status" :text="run.status_text" /></td>
            <td class="mono clip" :title="run.cfg && run.cfg.data">
              {{ (run.cfg && run.cfg.data) || '—' }}
            </td>
            <td>
              {{ (run.cfg && run.cfg.agent) || '—' }}
              <div class="muted">{{ (run.cfg && run.cfg.model) || '—' }}</div>
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
                <RouterLink class="btn sm" :to="`/agent/${run.run_id}`">详情</RouterLink>
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
