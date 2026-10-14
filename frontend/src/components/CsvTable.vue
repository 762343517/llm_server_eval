<script setup>
defineProps({
  data: { type: Object, default: null },
  loading: { type: Boolean, default: false },
  error: { type: String, default: '' },
})
const emit = defineEmits(['update:limit'])

const LIMITS = [200, 500, 1000, 2000, 5000]
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
      <div v-else class="table-wrap tall">
        <table>
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
            <tr v-for="(row, r) in data.rows" :key="r">
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
