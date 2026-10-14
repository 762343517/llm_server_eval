import { fileURLToPath, URL } from 'node:url'
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 构建产物直接输出到 backend/webui/，由 FastAPI 托管；开发态用 /api 代理到后端
export default defineConfig({
  plugins: [vue()],
  build: {
    outDir: fileURLToPath(new URL('../backend/webui', import.meta.url)),
    emptyOutDir: true,
    chunkSizeWarningLimit: 900,
  },
  server: {
    port: 5173,
    proxy: { '/api': { target: 'http://127.0.0.1:8000', changeOrigin: true } },
  },
})
