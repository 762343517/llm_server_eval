<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { RouterLink } from 'vue-router'
import { api } from '../api'
import { Card, CsvTable } from '../components/index.js'

// source: workspace（工作区文件）| accuracy（评测逐题 CSV）| stress（压测逐请求 CSV）
const props = defineProps({
  source: { type: String, default: 'workspace' },
  id: { type: String, default: '' },
  path: { type: String, default: '' },
})

const data = ref(null)
const loading = ref(false)
const error = ref('')
const limit = ref(500)

const fileName = computed(() => (props.path || '').split('/').pop() || props.id)
const title = computed(() =>
  props.source === 'workspace' ? `CSV 预览 · ${fileName.value}` : `结果表格 · ${props.id}`,
)
const backTo = computed(() => {
  if (props.source === 'accuracy') return `/accuracy/${props.id}`
  if (props.source === 'stress') return `/stress/${props.id}`
  return '/accuracy'
})
const downloadUrl = computed(() => {
  if (props.source === 'accuracy') return `/api/accuracy/runs/${props.id}/download.csv`
  if (props.source === 'stress') return `/api/stress/runs/${props.id}/download.csv`
  return api.csvDownload(props.path)
})

const load = async () => {
  loading.value = true
  error.value = ''
  try {
    if (props.source === 'accuracy') data.value = await api.accuracyCsv(props.id, limit.value)
    else if (props.source === 'stress') data.value = await api.stressCsv(props.id, limit.value)
    else data.value = await api.csv(props.path, limit.value)
  } catch (e) {
    error.value = e.message
    data.value = null
  } finally {
    loading.value = false
  }
}

const setLimit = (value) => {
  limit.value = value
  load()
}

onMounted(load)
watch(() => [props.path, props.id, props.source], load)
</script>

<template>
  <div class="page-head">
    <div>
      <h1>{{ title }}</h1>
      <div class="sub">
        支持预览工作区内 CSV；结果文件由后端按需导出（含 UTF-8 BOM，可直接用 Excel 打开）
      </div>
    </div>
    <div class="actions">
      <RouterLink class="btn" :to="backTo">返回</RouterLink>
      <a class="btn primary" :href="downloadUrl">下载文件</a>
    </div>
  </div>

  <Card title="数据预览">
    <CsvTable
      :data="data"
      :loading="loading"
      :error="error"
      :fixed="source === 'accuracy'"
      @update:limit="setLimit"
    />
  </Card>
</template>
