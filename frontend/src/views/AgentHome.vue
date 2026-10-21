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
// 任务选择方式：后端只认 limit/seed/tasks 三个字段，这里收敛成三种直观模式
//   all    全部任务        -> limit=0
//   random 随机抽取 N 个   -> limit=N + seed（0 = 每次启动都换一批）
//   pick   手动勾选        -> tasks=勾选的任务名
const MODES = [
  { value: 'all', label: '全部任务' },
  { value: 'random', label: '随机抽取' },
  { value: 'pick', label: '手动勾选' },
]
const mode = ref('random')
const picked = ref([])
const filter = ref('')
// 环境不可用时后端会直接拒绝启动，这里提前给出提示
const envReady = computed(() => !!env.value.ok)
const modelFree = computed(() => ['oracle', 'nop'].includes(form.agent))
const selectedDataset = computed(
  () => datasets.value.find((d) => (d.agent || {}).tasks_dir === form.data) || null,
)
const imageReady = computed(() => tasks.value.filter((t) => t.image_ready).length)
const pickedSet = computed(() => new Set(picked.value))
// 任务清单过滤：DeepSWE 有一百多个任务，手动勾选前需要能检索
const visibleTasks = computed(() => {
  const q = filter.value.trim().toLowerCase()
  if (!q) return tasks.value
  return tasks.value.filter((t) => [t.name, t.title, t.category, t.language]
    .some((v) => String(v || '').toLowerCase().includes(q)))
})
const allPicked = computed(() => visibleTasks.value.length > 0
  && visibleTasks.value.every((t) => pickedSet.value.has(t.name)))
const modeText = computed(() => {
  if (mode.value === 'all') return `将执行全部 ${taskTotal.value} 个任务`
  if (mode.value === 'random') {
    const n = Math.max(0, Number(form.limit) || 0)
    if (!n) return `将执行全部 ${taskTotal.value} 个任务（不抽样）`
    const seed = Number(form.seed) || 0
    return `从 ${taskTotal.value} 个任务里随机抽 ${n} 个（${seed > 0 ? `种子 ${seed}，可复现` : '每次启动都换一批'}）`
  }
  return picked.value.length ? `已选 ${picked.value.length} 个任务` : '尚未勾选任务'
})

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
  // 上次保存的任务集：后端登记过的记的是数据集名称（见 adapters.tasks.key_for），
  // 未登记的自定义路径才是绝对路径。统一换算成下拉框的 tasks_dir 值，
  // 换算不出来（自定义路径）就回填到自定义框，否则下拉框会显示空白
  const known = datasets.value.map((x) => (x.agent || {}).tasks_dir)
  const byKey = (key) => {
    const hit = datasets.value.find((x) => x.name === key || (x.agent || {}).tasks_dir === key)
    return hit ? (hit.agent || {}).tasks_dir || '' : ''
  }
  const resolved = d.data ? byKey(d.data) : ''
  const custom = d.data && !resolved ? d.data : ''
  Object.assign(form, {
    agent: d.agent || 'mini-swe-agent',
    endpoint: d.endpoint || '',
    api_key: d.api_key || '',
    model: d.model || '',
    model_class: d.model_class || 'litellm',
    data: custom ? known[0] || '' : (resolved || d.data || ''),
    data_custom: custom,
    tasks: '',
    limit: d.limit ?? 1,
    seed: d.seed ?? 0,
    concurrency: d.concurrency ?? 2,
    timeout_multiplier: d.timeout_multiplier ?? 1,
    verifier_timeout_multiplier: d.verifier_timeout_multiplier ?? 1,
  })
  // 恢复上次的模式：limit=0 视为「全部」，其余按「随机」回填。
  // 「手动勾选」是一次性复现入口（后端也刻意不持久化 tasks），因此从不恢复。
  mode.value = (d.limit ?? 1) === 0 ? 'all' : 'random'
  picked.value = []
  filter.value = ''
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
    picked.value = []                 // 换了任务集，旧勾选可能已不存在，直接清空
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

const newSeed = () => Math.floor(Math.random() * 1e9) + 1

const setMode = (value) => {
  mode.value = value
  if (value === 'pick') showTasks.value = true   // 勾选前先把清单露出来
}

const togglePick = (name) => {
  if (mode.value !== 'pick') setMode('pick')     // 直接勾选即视为「手动选择」
  const i = picked.value.indexOf(name)
  if (i >= 0) picked.value.splice(i, 1)
  else picked.value.push(name)
}

// 全选只作用于当前过滤结果，过滤后「全选」才符合直觉
const togglePickAll = () => {
  const names = visibleTasks.value.map((t) => t.name)
  if (allPicked.value) picked.value = picked.value.filter((n) => !names.includes(n))
  else picked.value = Array.from(new Set([...picked.value, ...names]))
}

const rerollSeed = () => {
  form.seed = newSeed()
  if (mode.value !== 'random') setMode('random')
}

const clearPicked = () => { picked.value = [] }

// 把三种模式翻译成后端认的 limit/seed/tasks 三个字段
const buildCfg = () => {
  const cfg = { ...form, data: form.data_custom || form.data }
  if (mode.value === 'all') {
    cfg.limit = 0                 // 0 = 全部
    cfg.seed = 0
    cfg.tasks = ''
  } else if (mode.value === 'random') {
    cfg.limit = Math.max(0, Number(form.limit) || 0)
    // limit=0 表示全部，后端不抽样，种子无意义；
    // 否则 seed=0 在后端是「按目录顺序取前 N 个」，要随机必须给非零种子，
    // 用户把种子留 0 时每次启动现场生成一个，即「每次换一批」
    cfg.seed = cfg.limit > 0
      ? (Number(form.seed) > 0 ? Number(form.seed) : newSeed())
      : 0
    cfg.tasks = ''
  } else {
    cfg.limit = picked.value.length
    cfg.seed = 0
    cfg.tasks = picked.value.join(',')
  }
  return cfg
}

const submit = async () => {
  error.value = ''
  notice.value = ''
  if (mode.value === 'pick' && !picked.value.length) {
    error.value = '手动勾选模式下请至少选择一个任务'
    return
  }
  submitting.value = true
  try {
    const { run_id: runId } = await api.agentStart(buildCfg())
    // 先把列表刷新掉：详情页导航万一失败（旧构建的按需加载 chunk 失效），
    // 至少本页能立刻看到刚提交的任务，不会表现为「点了没反应、列表也没有」。
    loadRuns()
    await router.push(`/agent/${runId}`).catch(() => {})
    // 导航被中止时（chunk 404 等）整页跳转兜底：浏览器会重新取 index.html 与新 chunk
    if (router.currentRoute.value.path !== `/agent/${runId}`) window.location.assign(`/agent/${runId}`)
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
        <span class="muted">任务选择：</span>
        <label
          v-for="m in MODES"
          :key="m.value"
          style="display: inline-flex; gap: 6px; align-items: center; cursor: pointer"
        >
          <input v-model="mode" type="radio" :value="m.value" @change="setMode(m.value)" />
          {{ m.label }}
        </label>
        <span class="muted">· {{ modeText }}</span>
      </div>
      <div class="row mt">
        <button class="btn sm" @click="showTasks = !showTasks">
          {{ showTasks ? '收起任务清单' : '查看任务清单' }}
        </button>
        <template v-if="showTasks">
          <input
            v-model.trim="filter"
            type="text"
            placeholder="过滤：名称 / 标题 / 类别 / 语言"
            style="max-width: 260px"
          />
          <span class="muted">
            共 {{ tasks.length }} 个<template v-if="filter">，匹配 {{ visibleTasks.length }} 个</template>
            · 已选 {{ picked.length }} 个
          </span>
          <button v-if="picked.length" class="btn sm" @click="clearPicked">清空选择</button>
        </template>
      </div>
      <div v-if="showTasks" class="table-wrap mt">
        <table v-if="visibleTasks.length">
          <thead>
            <tr>
              <th class="center" style="width: 34px">
                <input
                  type="checkbox"
                  :checked="allPicked"
                  title="全选当前筛选结果"
                  @change="togglePickAll"
                />
              </th>
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
            <tr v-for="t in visibleTasks" :key="t.name">
              <td class="center">
                <input type="checkbox" :checked="pickedSet.has(t.name)" @change="togglePick(t.name)" />
              </td>
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
        <div v-else class="empty">
          {{ tasks.length ? '没有匹配的任务，换个关键字试试' : '暂无任务（请检查任务集路径）' }}
        </div>
      </div>
    </Card>

    <Card title="运行参数" nested>
      <div class="grid">
        <template v-if="mode === 'random'">
          <div class="field">
            <label>随机抽取数量（0 = 全部）</label>
            <input v-model.number="form.limit" type="number" min="0" />
          </div>
          <div class="field">
            <label>随机种子（0 = 每次启动都换一批）</label>
            <div class="row">
              <input v-model.number="form.seed" type="number" min="0" style="max-width: 160px" />
              <button class="btn sm" @click="rerollSeed">随机换一个</button>
            </div>
            <span class="tip">填非零值即可复现同一批任务</span>
          </div>
        </template>
        <div v-else class="field span-2">
          <label>任务范围</label>
          <span class="tip">{{ modeText }}</span>
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
            <td class="num">
              {{ run.pass_at_1 === null || run.pass_at_1 === undefined ? '—' : pct(run.pass_at_1) }}
              <div v-if="run.summary && run.summary.num_excluded" class="muted">
                剔除环境/超时 {{ int(run.summary.num_excluded) }}
              </div>
            </td>
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
