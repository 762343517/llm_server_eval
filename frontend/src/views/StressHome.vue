<script setup>
import { computed, onMounted, onUnmounted, reactive, ref } from 'vue'
import { RouterLink, useRouter } from 'vue-router'
import { api } from '../api'
import { store } from '../store'
import { ProgressBar, StatTile, StatusBadge } from '../components/index.js'
import { compact, duration, int, isRunning, num, pct, shortFixed } from '../format'

const router = useRouter()
const form = reactive({})
const runs = ref([])
const error = ref('')
const submitting = ref(false)
const busyId = ref('')
let timer = null

const evalscope = computed(() => store.evalscope)
const hasRunning = computed(() => runs.value.some((r) => isRunning(r.status)))

const levels = computed(() =>
  String(form.ramp_levels_text || '')
    .replace(/，/g, ',')
    .split(',')
    .map((s) => parseInt(s.trim(), 10))
    .filter((n) => Number.isFinite(n) && n > 0),
)

// 长序列模式：序列长度 / 固定前缀 / 变长后缀 / Prefix Cache 命中率
const isLong = computed(() => form.prompt_mode === 'long')
const hasCustomPrefix = computed(() => Boolean(String(form.prefix_content || '').trim()))

// 阶梯加压时该值是每一档的请求/会话数，固定并发时是单个并发下的数量
const iterationsLabel = computed(() => {
  const unit = isLong.value ? '请求数' : '会话数'
  return form.concurrency_mode === 'ramp' ? `每档并发${unit}` : `每个并发${unit}`
})

// 与后端 perf_core.parse_tokens 同口径：支持 128000 / 128K / 1M（K=1000、M=1000000）
const parseTokens = (value) => {
  if (value === null || value === undefined || value === '') return 0
  if (typeof value === 'number') return Math.max(0, Math.trunc(value))
  const text = String(value).trim().toLowerCase().replace(/[, _]/g, '')
  const matched = text.match(/^(\d+(?:\.\d+)?)([km]?)$/)
  if (!matched) return 0
  const mult = matched[2] === 'm' ? 1000000 : matched[2] === 'k' ? 1000 : 1
  return Math.max(0, Math.trunc(parseFloat(matched[1]) * mult))
}

const seqLength = computed(() => parseTokens(form.seq_length))
const prefixLength = computed(() => {
  const total = seqLength.value
  const fixed = parseTokens(form.prefix_length)
  if (fixed > 0) return Math.min(fixed, total)
  const ratio = Math.max(0, Math.min(1, Number(form.prefix_cache_ratio) || 0))
  return Math.trunc(total * ratio)
})
const suffixLength = computed(() => Math.max(0, seqLength.value - prefixLength.value))
const hitRatio = computed(() =>
  seqLength.value ? prefixLength.value / seqLength.value : 0,
)

const applyPreset = (totalTokens, ratio) => {
  form.prompt_mode = 'long'
  form.seq_length = totalTokens
  form.output_length = 1024
  form.prefix_length = 0
  form.prefix_cache_ratio = ratio
}

const isLongCfg = (cfg) => Boolean(cfg && cfg.prompt_mode === 'long')

// 加压方式以快照的 concurrency_mode 为准：两种方式下另一组的字段都是表单残留值
const isRampCfg = (cfg) => {
  if (!cfg) return false
  if (cfg.concurrency_mode) return cfg.concurrency_mode === 'ramp'
  return Array.isArray(cfg.ramp_levels) && cfg.ramp_levels.length > 0
}

const expected = computed(() => {
  const users =
    form.concurrency_mode === 'ramp'
      ? levels.value.reduce((a, b) => a + b, 0)
      : Number(form.concurrent_users) || 0
  const turns = isLong.value ? 1 : Number(form.turns) || 1
  return users * (Number(form.iterations) || 0) * turns
})

const applyDefaults = () => {
  const d = store.stressDefaults
  Object.assign(form, { ...d })
}

const loadRuns = async () => {
  try {
    const data = await api.stressRuns()
    runs.value = data.runs || []
  } catch (e) {
    error.value = e.message
  }
}

const submit = async () => {
  error.value = ''
  submitting.value = true
  try {
    const { run_id: runId } = await api.stressStart({ ...form })
    router.push(`/stress/${runId}`)
  } catch (e) {
    error.value = e.message
  } finally {
    submitting.value = false
  }
}

const stopRun = async (run) => {
  if (!window.confirm(`确认停止压测 ${run.run_id}？已完成的部分结果会保留。`)) return
  busyId.value = run.run_id
  try {
    await api.stressStop(run.run_id)
    await loadRuns()
  } catch (e) {
    error.value = e.message
  } finally {
    busyId.value = ''
  }
}

const deleteRun = async (run) => {
  if (!window.confirm(`确认删除压测任务 ${run.run_id}？数据集与结果文件会一并删除。`)) return
  busyId.value = run.run_id
  try {
    await api.stressDelete(run.run_id)
    await loadRuns()
  } catch (e) {
    error.value = e.message
  } finally {
    busyId.value = ''
  }
}

onMounted(async () => {
  await store.load()
  applyDefaults()
  await loadRuns()
  timer = setInterval(() => {
    if (hasRunning.value) loadRuns()
  }, 5000)
})
onUnmounted(() => timer && clearInterval(timer))
</script>

<template>
  <div class="page-head">
    <div>
      <h1>压力测试</h1>
      <div class="sub">调用 evalscope 对本地模型服务压测：并发 / 阶梯加压、首字延迟、吞吐与缓存命中</div>
    </div>
    <div class="actions">
      <button class="btn" @click="loadRuns">刷新列表</button>
    </div>
  </div>

  <div v-if="error" class="alert bad">{{ error }}</div>
  <div v-if="!evalscope.available" class="alert warn">
    evalscope 当前不可用：{{ evalscope.error || '未找到可执行文件' }}。
    安装后可自动识别（pip install evalscope），或用环境变量 EVALSCOPE_BIN 指定完整路径。
  </div>

  <section class="card">
    <h2>新建压测任务</h2>
    <p class="hint">
      evalscope 请求 <code>/chat/completions</code>。此处可填写到端口（如
      <code>http://10.174.0.70:34000</code>），或已含 <code>/v1</code> 前缀的地址，
      缺失的 <code>/chat/completions</code> 会自动补全。
    </p>

    <div class="grid">
      <div class="field">
        <label>任务名称</label>
        <input v-model.trim="form.name" type="text" />
      </div>
      <div class="field span-2">
        <label>说明（可选）</label>
        <input v-model.trim="form.description" type="text" />
      </div>
      <div class="field span-2">
        <label>服务端点</label>
        <input v-model.trim="form.endpoint" type="text" placeholder="http://host:port" />
      </div>
      <div class="field">
        <label>API Key（可留空）</label>
        <input v-model.trim="form.api_key" type="password" autocomplete="off" />
      </div>
      <div class="field">
        <label>模型名</label>
        <input v-model.trim="form.model" type="text" />
      </div>
      <div class="field">
        <label>请求超时（秒）</label>
        <input v-model.number="form.timeout" type="number" min="1" max="3600" />
      </div>
    </div>

    <h2 class="mt">请求场景</h2>
    <div class="grid">
      <div class="field span-2">
        <label>请求模式</label>
        <select v-model="form.prompt_mode">
          <option value="chat">自定义会话提问（多轮 / 会话唯一前缀）</option>
          <option value="long">长序列 + 固定前缀（Prefix Cache 命中率）</option>
        </select>
        <span class="tip">
          长序列模式按 token 精确控制序列长度，并让所有请求共享同一段固定前缀，
          用于 Prefix Cache 命中率与长上下文性能测试
        </span>
      </div>
    </div>

    <template v-if="!isLong">
      <div class="grid wide">
        <div class="field">
          <label>系统提示词（可选）</label>
          <textarea v-model="form.system_prompt" placeholder="留空则不发送 system 消息"></textarea>
        </div>
        <div class="field">
          <label>提问内容</label>
          <textarea v-model="form.prompt"></textarea>
          <span class="tip">多轮场景会重复这条提问，历史中带上模型上一轮回答</span>
        </div>
      </div>
      <div class="grid mt">
        <div class="field">
          <label>对话轮数</label>
          <input v-model.number="form.turns" type="number" min="1" max="100" />
        </div>
        <div class="field">
          <label>会话唯一前缀</label>
          <label class="check">
            <input v-model="form.unique_prefix" type="checkbox" />
            <span>每会话拼接唯一前缀（关闭前缀缓存，测真实冷启动）</span>
          </label>
        </div>
      </div>
    </template>

    <template v-else>
      <div class="row mt">
        <span class="muted">按测试方案预设：</span>
        <button class="btn sm" @click="applyPreset('128K', 0.5)">128K · 命中率 50%</button>
        <button class="btn sm" @click="applyPreset('128K', 0.9)">128K · 命中率 90%</button>
        <button class="btn sm" @click="applyPreset('1024K', 0.5)">1024K · 命中率 50%</button>
        <button class="btn sm" @click="applyPreset('1024K', 0.9)">1024K · 命中率 90%</button>
      </div>
      <div class="grid mt">
        <div class="field span-2">
          <label>分词器路径</label>
          <input
            v-model.trim="form.tokenizer_path"
            type="text"
            placeholder="/home/wtc/prj/zhipuEval/model/GLM-5.3"
          />
          <span class="tip">
            evalscope 按 token 精确生成长序列，需与被测模型同一词表的分词器；
            目录须含对话模板（chat_template.jinja 或 tokenizer_config.json 的 chat_template）
          </span>
        </div>
        <div class="field">
          <label>序列长度（输入 tokens）</label>
          <input v-model.trim="form.seq_length" type="text" placeholder="128000" />
          <span class="tip">支持 128000 / 128K / 1M（K=1000、M=1000000）</span>
        </div>
        <div class="field">
          <label>输出长度（tokens）</label>
          <input v-model.number="form.output_length" type="number" min="1" max="131072" />
        </div>
        <div class="field">
          <label>Prefix Cache 命中率</label>
          <input
            v-model.number="form.prefix_cache_ratio"
            type="number"
            step="0.05"
            min="0"
            max="1"
          />
          <span class="tip">固定前缀长度 = 序列长度 × 命中率</span>
        </div>
        <div class="field">
          <label>固定前缀长度（tokens，0 = 用命中率）</label>
          <input v-model.number="form.prefix_length" type="number" min="0" />
          <span class="tip">填写后优先于命中率，可用于精确对齐方案的 token 数</span>
        </div>
        <div class="field">
          <label>预热请求数（0 = 关闭）</label>
          <input v-model.number="form.warmup_num" type="number" min="0" max="100000" />
          <span class="tip">建议不小于并发数，先把共享前缀写入缓存再统计</span>
        </div>
        <div class="field span-2">
          <label>强制输出长度</label>
          <label class="check">
            <input v-model="form.ignore_eos" type="checkbox" />
            <span>ignore_eos：忽略停止符按输出长度生成，TPOT 口径更稳定</span>
          </label>
        </div>
      </div>
      <div class="grid wide mt">
        <div class="field">
          <label>固定前缀内容（可选）</label>
          <textarea
            v-model="form.prefix_content"
            placeholder="留空：由 evalscope 生成所有请求共享的随机前缀，长度精确可控；填写：按该内容注入固定前缀，长度自动 = 序列长度 − 提问长度"
          ></textarea>
          <span class="tip">
            留空时走 random 数据集：请求内容由分词器随机 token 生成，不含业务语义，
            因此不需要填提问内容；填写后走 line_by_line，提问内容作为每条请求的后缀
          </span>
        </div>
        <div v-if="hasCustomPrefix" class="field">
          <label>提问内容</label>
          <textarea
            v-model="form.prompt"
            placeholder="作为每条请求的可变后缀；留空则使用默认提问"
          ></textarea>
          <span class="tip">各请求的前缀一致，后缀为这条提问（多条请求会带上序号）</span>
        </div>
      </div>
    </template>

    <div class="grid mt">
      <div class="field">
        <label>max_tokens</label>
        <input v-model.number="form.max_tokens" type="number" min="1" max="131072" />
      </div>
      <div class="field">
        <label>temperature</label>
        <input v-model.number="form.temperature" type="number" step="0.1" min="0" max="2" />
      </div>
      <div class="field">
        <label>top_p</label>
        <input v-model.number="form.top_p" type="number" step="0.05" min="0.01" max="1" />
      </div>
      <div class="field">
        <label>top_k（-1 = 不发送）</label>
        <input v-model.number="form.top_k" type="number" min="-1" max="200" />
      </div>
    </div>

    <h2 class="mt">负载模型</h2>
    <div class="grid">
      <div class="field">
        <label>加压方式</label>
        <select v-model="form.concurrency_mode">
          <option value="fixed">固定并发</option>
          <option value="ramp">阶梯加压</option>
        </select>
      </div>
      <template v-if="form.concurrency_mode === 'fixed'">
        <div class="field">
          <label>并发用户数</label>
          <input v-model.number="form.concurrent_users" type="number" min="1" max="1024" />
        </div>
      </template>
      <template v-else>
        <div class="field">
          <label>并发档位（逗号分隔，自动升序）</label>
          <input v-model.trim="form.ramp_levels_text" type="text" placeholder="2, 4, 8, 16" />
        </div>
        <div class="field">
          <label>档位间隔（秒，0 = 跑完即换档）</label>
          <input v-model.number="form.ramp_pause_seconds" type="number" min="0" max="3600" />
        </div>
      </template>
      <div class="field">
        <label>{{ iterationsLabel }}</label>
        <input v-model.number="form.iterations" type="number" min="1" max="10000" />
      </div>
    </div>

    <div class="stats mt">
      <StatTile label="预计请求数" :value="int(expected)" sub="次" />
      <StatTile label="并发档位" :value="form.concurrency_mode === 'ramp' ? levels.join(' / ') || '—' : form.concurrent_users" />
      <template v-if="isLong">
        <StatTile label="序列长度" :value="compact(seqLength)" sub="tokens" />
        <StatTile
          label="固定前缀"
          :value="hasCustomPrefix ? '按内容自动' : compact(prefixLength)"
          :sub="hasCustomPrefix ? '长度 = 序列 − 提问' : 'tokens'"
        />
        <StatTile
          v-if="!hasCustomPrefix"
          label="变长后缀"
          :value="compact(suffixLength)"
          sub="tokens"
        />
        <StatTile
          v-if="!hasCustomPrefix"
          label="Prefix Cache 命中率"
          :value="pct(hitRatio)"
        />
        <StatTile label="输出长度" :value="int(form.output_length)" sub="tokens" />
      </template>
      <template v-else>
        <StatTile label="对话轮数" :value="int(form.turns)" />
        <StatTile label="单请求上限" :value="int(form.max_tokens)" sub="tokens" />
      </template>
    </div>

    <div class="row mt">
      <button class="btn primary" :disabled="submitting || !evalscope.available" @click="submit">
        {{ submitting ? '正在启动…' : '启动压测' }}
      </button>
      <span class="muted">启动后进入任务详情页，可实时查看 RPS / 延迟曲线与逐请求明细。</span>
    </div>
  </section>

  <section class="card">
    <h2>压测任务</h2>
    <p class="hint">共 {{ runs.length }} 个任务{{ hasRunning ? ' · 有任务正在运行，列表每 5 秒自动刷新' : '' }}</p>
    <div v-if="!runs.length" class="empty">暂无压测任务</div>
    <div v-else class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>任务</th>
            <th>状态</th>
            <th>模型 / 端点</th>
            <th>负载</th>
            <th>进度</th>
            <th class="num">RPS</th>
            <th class="num">成功率</th>
            <th class="num">TTFT 均值 / P95</th>
            <th class="num">耗时</th>
            <th style="width: 210px">操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="run in runs" :key="run.run_id">
            <td>
              <RouterLink :to="`/stress/${run.run_id}`" class="mono">{{ run.run_id }}</RouterLink>
              <div class="muted">{{ run.started_at_str }}</div>
            </td>
            <td><StatusBadge :status="run.status" :text="run.status_text" /></td>
            <td>
              {{ (run.cfg && run.cfg.model) || '—' }}
              <div class="muted mono">{{ (run.cfg && run.cfg.endpoint) || '—' }}</div>
            </td>
            <td class="nowrap">
              <template v-if="isRampCfg(run.cfg)">
                阶梯 {{ (run.cfg.ramp_levels || []).join('/') || '—' }}
              </template>
              <template v-else>并发 {{ (run.cfg && run.cfg.concurrent_users) || '—' }}</template>
              <div v-if="isLongCfg(run.cfg)" class="muted">
                序列 {{ run.cfg.seq_length }} tokens ·
                {{ run.cfg.prefix_content_chars ? '固定前缀内容' : `命中率 ${pct(run.cfg.prefix_cache_ratio)}` }}
              </div>
              <div v-else class="muted">×{{ (run.cfg && run.cfg.iterations) || '—' }} 轮，{{ (run.cfg && run.cfg.turns) || 1 }} 轮对话</div>
            </td>
            <td style="min-width: 170px">
              <div class="muted">{{ (run.progress && run.progress.stage) || '' }}</div>
              <ProgressBar
                :pct="(run.progress && run.progress.pct) || 0"
                :status="run.status"
                :text="`${((run.progress && run.progress.pct) || 0).toFixed(0)}%`"
              />
            </td>
            <td class="num">{{ num((run.stats || {}).requests_per_second, 2) }}</td>
            <td class="num">{{ pct((run.stats || {}).success_rate) }}</td>
            <td class="num">
              {{ shortFixed((run.stats || {}).ttft_mean_s, 3) }} /
              {{ shortFixed((run.stats || {}).ttft_p95_s, 3) }}
            </td>
            <td class="num">{{ duration(run.duration_s) }}</td>
            <td>
              <div class="row">
                <RouterLink class="btn sm" :to="`/stress/${run.run_id}`">详情</RouterLink>
                <button
                  v-if="isRunning(run.status)"
                  class="btn sm danger"
                  :disabled="busyId === run.run_id"
                  @click="stopRun(run)"
                >
                  停止
                </button>
                <button class="btn sm danger" :disabled="busyId === run.run_id" @click="deleteRun(run)">
                  删除
                </button>
              </div>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </section>
</template>
