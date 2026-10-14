<script setup>
import { nextTick, ref, watch } from 'vue'

const props = defineProps({
  logs: { type: Array, default: () => [] },
  follow: { type: Boolean, default: true },
})

const box = ref(null)

const lineClass = (msg) => {
  const text = String(msg || '')
  if (/\[异常\]|\[失败\]|Traceback|Error/.test(text)) return 'line err'
  if (/\[停止\]|\[中断\]|警告|WARN/.test(text)) return 'line warn'
  return 'line'
}

watch(
  () => props.logs.length,
  async () => {
    if (!props.follow) return
    await nextTick()
    if (box.value) box.value.scrollTop = box.value.scrollHeight
  },
)
</script>

<template>
  <div ref="box" class="logs">
    <div v-if="!logs.length" class="muted">暂无日志</div>
    <div v-for="(item, i) in logs" :key="i" :class="lineClass(item.msg)">
      <span class="t">{{ item.t }}</span>{{ item.msg }}
    </div>
  </div>
</template>
