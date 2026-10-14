<script setup>
import { computed, onMounted } from 'vue'
import { RouterLink, RouterView, useRoute } from 'vue-router'
import { store } from './store'

const route = useRoute()
onMounted(() => store.load())

const activeTab = computed(() => {
  if (route.path.startsWith('/stress')) return 'stress'
  if (route.path.startsWith('/accuracy') || route.path.startsWith('/csv')) return 'accuracy'
  return ''
})

const evalscope = computed(() => store.evalscope)
const evalscopeText = computed(() => {
  if (evalscope.value.available) return `evalscope ${evalscope.value.version || '可用'}`
  return evalscope.value.path ? 'evalscope 不可用' : 'evalscope 未安装'
})
</script>

<template>
  <header class="topbar">
    <div class="brand">
      SimpleQA-Verified 评测平台
      <small>精度测试 · 压力测试</small>
    </div>
    <nav class="tabs">
      <RouterLink class="tab" :class="{ active: activeTab === 'accuracy' }" to="/accuracy">
        精度测试
      </RouterLink>
      <RouterLink class="tab" :class="{ active: activeTab === 'stress' }" to="/stress">
        压力测试
      </RouterLink>
      <span
        class="meta-chip"
        :class="evalscope.available ? 'ok' : 'bad'"
        :title="evalscope.error || evalscope.path || ''"
      >
        {{ evalscopeText }}
      </span>
    </nav>
  </header>

  <main class="wrap">
    <div v-if="store.error" class="alert bad">加载应用元信息失败：{{ store.error }}</div>
    <RouterView />
  </main>
</template>
