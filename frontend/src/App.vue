<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { RouterLink, RouterView, useRoute } from 'vue-router'
import { store } from './store'

const route = useRoute()

const NAV = [
  {
    key: 'accuracy',
    to: '/accuracy',
    label: '精度测试',
    // 靶心图标（单 path，多个子路径拼接）
    icon: 'M12 3a9 9 0 1 0 0 18a9 9 0 1 0 0-18M12 7a5 5 0 1 0 0 10a5 5 0 1 0 0-10M12 11a1 1 0 1 0 0 2a1 1 0 1 0 0-2',
  },
  {
    key: 'stress',
    to: '/stress',
    label: '压力测试',
    icon: 'M3 12h4l2.5-6 4 12 2.5-6h5',
  },
  {
    key: 'agent',
    to: '/agent',
    label: 'Agent 评测',
    icon: 'M12 3v4M5 8h14a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-8a2 2 0 0 1 2-2M9 13v1M15 13v1',
  },
]

const activeTab = computed(() => {
  if (route.path.startsWith('/agent')) return 'agent'
  if (route.path.startsWith('/stress')) return 'stress'
  if (route.path.startsWith('/accuracy') || route.path.startsWith('/csv')) return 'accuracy'
  return ''
})
const currentNav = computed(() => NAV.find((i) => i.key === activeTab.value) || NAV[0])

// 侧栏：桌面为可收起的窄图标栏（状态存 localStorage），移动端为抽屉
const collapsed = ref(localStorage.getItem('nav-collapsed') === '1')
const isMobile = ref(window.innerWidth <= 960)
const drawerOpen = ref(false)

const syncViewport = () => {
  isMobile.value = window.innerWidth <= 960
  if (!isMobile.value) drawerOpen.value = false
}
const toggleNav = () => {
  if (isMobile.value) {
    drawerOpen.value = !drawerOpen.value
    return
  }
  collapsed.value = !collapsed.value
  localStorage.setItem('nav-collapsed', collapsed.value ? '1' : '0')
}

// 切换页面后自动收起移动端抽屉
watch(() => route.fullPath, () => { drawerOpen.value = false })

onMounted(() => {
  store.load()
  window.addEventListener('resize', syncViewport)
})
onUnmounted(() => window.removeEventListener('resize', syncViewport))

const evalscope = computed(() => store.evalscope)
const evalscopeText = computed(() => {
  if (evalscope.value.available) return `evalscope ${evalscope.value.version || '可用'}`
  return evalscope.value.path ? 'evalscope 不可用' : 'evalscope 未安装'
})
const evalscopeTitle = computed(() => evalscope.value.error || evalscope.value.path || evalscopeText.value)

// Agent 评测依赖 docker + pier，侧栏底部只展示最关键的两项
const agentEnv = computed(() => store.agentEnv)
const agentEnvOk = computed(() => !!agentEnv.value.ok)
const agentEnvText = computed(() => {
  const env = agentEnv.value
  if (!env.pier) return 'pier 未就绪'
  const pier = env.pier.available ? `pier ${env.pier.version || '可用'}` : 'pier 未安装'
  const docker = env.docker && env.docker.ok ? `docker ${env.docker.version}` : 'docker 不可用'
  return `${pier} · ${docker}`
})
const agentEnvTitle = computed(() =>
  (agentEnv.value.notes || []).join('\n') || agentEnvText.value)
</script>

<template>
  <div class="app-shell" :class="{ collapsed: collapsed && !isMobile, 'drawer-open': drawerOpen }">
    <aside class="sidebar">
      <div class="side-brand">
        <span class="logo">模</span>
        <div class="side-brand-text">
          <b>大模型服务测试平台</b>
          <small>精度 · 压力 · Agent</small>
        </div>
      </div>

      <nav class="side-nav">
        <RouterLink
          v-for="item in NAV"
          :key="item.key"
          class="side-link"
          :class="{ active: activeTab === item.key }"
          :to="item.to"
          :title="item.label"
        >
          <svg
            class="ico"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            stroke-width="1.7"
            stroke-linecap="round"
            stroke-linejoin="round"
            aria-hidden="true"
          >
            <path :d="item.icon" />
          </svg>
          <span class="label">{{ item.label }}</span>
        </RouterLink>
      </nav>

      <div class="side-foot">
        <div class="side-status" :class="agentEnvOk ? 'ok' : 'bad'" :title="agentEnvTitle">
          <i class="dot"></i><span>{{ agentEnvText }}</span>
        </div>
        <div class="side-status" :class="evalscope.available ? 'ok' : 'bad'" :title="evalscopeTitle">
          <i class="dot"></i><span>{{ evalscopeText }}</span>
        </div>
      </div>
    </aside>

    <div v-if="drawerOpen" class="drawer-mask" @click="drawerOpen = false"></div>

    <div class="app-body">
      <header class="topbar">
        <button
          class="nav-toggle"
          type="button"
          :title="isMobile ? (drawerOpen ? '关闭导航' : '打开导航') : (collapsed ? '展开导航' : '收起导航')"
          @click="toggleNav"
        >
          <svg
            class="ico"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            stroke-width="1.8"
            stroke-linecap="round"
            aria-hidden="true"
          >
            <path d="M4 7h16M4 12h16M4 17h16" />
          </svg>
        </button>
        <div class="crumbs">
          <span class="muted">测试平台</span>
          <span class="sep">/</span>
          <b>{{ currentNav.label }}</b>
        </div>
        <div class="topbar-right">
          <span class="meta-chip" :class="agentEnvOk ? 'ok' : 'bad'" :title="agentEnvTitle">
            {{ agentEnvText }}
          </span>
          <span class="meta-chip" :class="evalscope.available ? 'ok' : 'bad'" :title="evalscopeTitle">
            {{ evalscopeText }}
          </span>
        </div>
      </header>

      <main class="wrap">
        <div v-if="store.error" class="alert bad">加载应用元信息失败：{{ store.error }}</div>
        <RouterView />
      </main>
    </div>
  </div>
</template>
