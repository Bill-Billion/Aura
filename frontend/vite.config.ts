import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import tailwindcss from '@tailwindcss/vite'
import { fileURLToPath, URL } from 'node:url'
import templateCompilerOptions from '@tresjs/core/template-compiler-options'

export default defineConfig({
  plugins: [vue(templateCompilerOptions), tailwindcss()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    port: 5173,
    proxy: {
      '/ws': { target: 'http://localhost:8000', ws: true },
      '/api': 'http://localhost:8000',
    }
  }
})
