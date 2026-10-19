// 后端接口封装：统一错误处理，错误信息优先取后端返回的 detail
const request = async (url, options = {}) => {
  const res = await fetch(url, options)
  let data = null
  try {
    data = await res.json()
  } catch (e) {
    data = null
  }
  if (!res.ok) {
    const detail = data && (data.detail || data.error)
    throw new Error(detail || `请求失败（HTTP ${res.status}）`)
  }
  return data
}

const post = (url, body) =>
  request(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {}),
  })

const query = (params) => {
  const search = new URLSearchParams()
  Object.entries(params).forEach(([k, v]) => {
    if (v !== undefined && v !== null && v !== '') search.set(k, v)
  })
  const text = search.toString()
  return text ? `?${text}` : ''
}

export const api = {
  meta: () => request('/api/meta'),

  // 全局测试队列：精度与压测共用，展示正在运行与排队中的任务
  queue: () => request('/api/queue'),

  csv: (path, limit) => request(`/api/csv${query({ path, limit })}`),
  csvDownload: (path) => `/api/csv/download${query({ path })}`,

  accuracyRuns: () => request('/api/accuracy/runs'),
  accuracyStart: (cfg) => post('/api/accuracy/runs', cfg),
  accuracyRun: (id) => request(`/api/accuracy/runs/${encodeURIComponent(id)}`),
  accuracyItems: (id, { limit = 500, grade = '', q = '' } = {}) =>
    request(`/api/accuracy/runs/${encodeURIComponent(id)}/items${query({ limit, grade, q })}`),
  accuracyStop: (id) => post(`/api/accuracy/runs/${encodeURIComponent(id)}/stop`),
  accuracyDelete: (id) =>
    request(`/api/accuracy/runs/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  accuracyCsv: (id, limit) =>
    request(`/api/accuracy/runs/${encodeURIComponent(id)}/csv${query({ limit })}`),

  stressRuns: () => request('/api/stress/runs'),
  stressStart: (cfg) => post('/api/stress/runs', cfg),
  stressSaveConfig: (cfg) => post('/api/stress/config', cfg),
  stressRun: (id) => request(`/api/stress/runs/${encodeURIComponent(id)}`),
  stressRecords: (id, { limit = 200, onlyErrors = false } = {}) =>
    request(
      `/api/stress/runs/${encodeURIComponent(id)}/records${query({
        limit,
        only_errors: onlyErrors ? 'true' : '',
      })}`,
    ),
  stressSeries: (id, buckets = 60) =>
    request(`/api/stress/runs/${encodeURIComponent(id)}/series${query({ buckets })}`),
  stressArgs: (id) => request(`/api/stress/runs/${encodeURIComponent(id)}/args`),
  stressFiles: (id) => request(`/api/stress/runs/${encodeURIComponent(id)}/files`),
  // 原始产物的直链（日志/汇总报告内联预览，报告 HTML 供 iframe 嵌入）
  stressFileUrl: (id, path, { download = false } = {}) =>
    `/api/stress/runs/${encodeURIComponent(id)}/file${query({ path, download: download ? 1 : '' })}`,
  stressStop: (id) => post(`/api/stress/runs/${encodeURIComponent(id)}/stop`),
  stressDelete: (id) =>
    request(`/api/stress/runs/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  stressCsv: (id, limit) =>
    request(`/api/stress/runs/${encodeURIComponent(id)}/csv${query({ limit })}`),

  // Agent 评测：pier + docker 逐任务起沙箱，agent 改代码后由任务自带测试判定
  agentEnv: () => request('/api/agent/env'),
  agentTasks: (data) => request(`/api/agent/tasks${query({ data })}`),
  agentRuns: () => request('/api/agent/runs'),
  agentStart: (cfg) => post('/api/agent/runs', cfg),
  agentSaveConfig: (cfg) => post('/api/agent/config', cfg),
  agentRun: (id) => request(`/api/agent/runs/${encodeURIComponent(id)}`),
  agentItems: (id, { limit = 200, status = '', kind = '', q = '' } = {}) =>
    request(`/api/agent/runs/${encodeURIComponent(id)}/items${query({ limit, status, kind, q })}`),
  // 子任务看板：等待 / 运行中 / 完成 / 异常退出
  agentSubtasks: (id) => request(`/api/agent/runs/${encodeURIComponent(id)}/subtasks`),
  // 子任务日志：offset<0 取尾部，之后用返回的 next 增量拉取
  agentTaskLog: (id, task, offset = -1) =>
    request(`/api/agent/runs/${encodeURIComponent(id)}/tasklog${query({ task, offset })}`),
  // 任务产物直链：测试输出 / 模型补丁 / 任务说明（内联文本预览，download=1 触发下载）
  agentFileUrl: (id, path, { download = false } = {}) =>
    `/api/agent/runs/${encodeURIComponent(id)}/file${query({ path, download: download ? 1 : '' })}`,
  agentStop: (id) => post(`/api/agent/runs/${encodeURIComponent(id)}/stop`),
  agentDelete: (id) =>
    request(`/api/agent/runs/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  agentCsv: (id, limit) =>
    request(`/api/agent/runs/${encodeURIComponent(id)}/csv${query({ limit })}`),
}
