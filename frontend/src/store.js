import { reactive } from 'vue'
import { api } from './api'

// 全局共享的应用元信息（表单默认值、选项、evalscope 可用性）
export const store = reactive({
  meta: null,
  error: '',
  loading: false,

  async load(force = false) {
    if (this.meta && !force) return this.meta
    this.loading = true
    try {
      this.meta = await api.meta()
      this.error = ''
    } catch (e) {
      this.error = e.message
    } finally {
      this.loading = false
    }
    return this.meta
  },

  get accuracyDefaults() {
    return (this.meta && this.meta.accuracy && this.meta.accuracy.defaults) || {}
  },
  get stressDefaults() {
    return (this.meta && this.meta.stress && this.meta.stress.defaults) || {}
  },
  get evalscope() {
    return (this.meta && this.meta.stress && this.meta.stress.evalscope) || {}
  },
  // 分词器候选：后端固定扫描项目 model/ 目录，页面只允许从这里选择
  get tokenizers() {
    return (this.meta && this.meta.stress && this.meta.stress.tokenizers) || []
  },
  get tokenizerDir() {
    return (this.meta && this.meta.stress && this.meta.stress.tokenizer_dir) || ''
  },
})
