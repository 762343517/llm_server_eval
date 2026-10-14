<script setup>
import { computed } from 'vue'

const props = defineProps({
  data: { type: Object, default: null },
  loading: { type: Boolean, default: false },
  error: { type: String, default: '' },
  // 结果表格：固定列宽 + 超宽换行（工作区/压测预览保持原有自适应宽度）
  fixed: { type: Boolean, default: false },
})
const emit = defineEmits(['update:limit'])

const LIMITS = [200, 500, 1000, 2000, 5000]

// 判定列（精度测试结果 CSV 导出为「判定」，英文为 grade）：正确整行绿底，错误红底，未作答黄底
const gradeIndex = computed(() => {
  const header = (props.data && props.data.header) || []
  return header.findIndex((h) => /^(判定|grade)$/i.test(String(h || '').trim()))
})

const rowClass = (row) => {
  if (gradeIndex.value < 0) return ''
  const v = String(row[gradeIndex.value] ?? '').trim().toLowerCase()
  if (v === 'correct' || v === '正确') return 'row-ok'
  if (v === 'incorrect' || v === '错误') return 'row-bad'
  if (v === 'not_attempted' || v === '未作答') return 'row-warn'
  return ''
}

// 固定列宽（仅 fixed=true 时生效）：按列名给出稳定宽度，避免长文本把列撑开
const COL_WIDTHS = {
  题号: '76px', original_index: '76px',
  判定: '96px', grade: '96px',
  问题: '320px', problem: '320px',
  模型回答: '360px', prediction: '360px',
  标准答案: '260px', answer: '260px',
  抽取答案: '180px', exact_answer: '180px',
  主题: '120px', topic: '120px',
  答案类型: '110px', answer_type: '110px',
  细分学科: '120px', subject: '120px',
  含图片: '90px', has_image: '90px',
}
const colWidth = (name) => COL_WIDTHS[String(name || '').trim()] || '130px'
</script>

<template>
  <div>
    <div v-if="error" class="alert bad">{{ error }}</div>
    <div v-else-if="loading" class="empty"><span class="spin"></span> 正在读取…</div>
    <template v-else-if="data">
      <div class="filters">
        <span class="mono muted">{{ data.path }}</span>
        <span class="muted">共 {{ data.total }} 行 · 显示前 {{ data.shown }} 行 · {{ data.encoding }}</span>
        <span class="muted nowrap">预览行数</span>
        <select
          :value="data.limit"
          @change="emit('update:limit', Number($event.target.value))"
        >
          <option v-for="n in LIMITS" :key="n" :value="n">{{ n }}</option>
        </select>
        <div class="card-actions"><slot name="actions"></slot></div>
      </div>

      <div v-if="!data.header.length" class="empty">文件为空</div>
      <div v-else class="table-wrap tall" :class="{ fixed }">
        <table>
          <colgroup v-if="fixed">
            <col style="width: 56px" />
            <col v-for="(col, i) in data.header" :key="i" :style="{ width: colWidth(col) }" />
          </colgroup>
          <thead>
            <tr>
              <th class="center">#</th>
              <th
                v-for="(col, i) in data.header"
                :key="i"
                :class="{ num: data.numeric[i] }"
              >
                {{ col || `列 ${i + 1}` }}
              </th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="(row, r) in data.rows" :key="r" :class="rowClass(row)">
              <td class="center muted">{{ r + 1 }}</td>
              <td
                v-for="(cell, c) in row"
                :key="c"
                :class="{ num: data.numeric[c] }"
                :title="String(cell)"
              >
                <span class="clip">{{ cell }}</span>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <p v-if="data.total > data.shown" class="muted mt">
        仅预览前 {{ data.shown }} 行，完整数据请下载文件查看。
      </p>
    </template>
    <div v-else class="empty">暂无数据</div>
  </div>
</template>
