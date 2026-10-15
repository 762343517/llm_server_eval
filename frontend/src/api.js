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
}
