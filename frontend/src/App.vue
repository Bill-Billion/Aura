<script setup lang="ts">
import { onMounted, computed } from 'vue'
import { useWebSocket } from '@/composables/useWebSocket'
import { useSimulationStore } from '@/stores/simulationStore'
import { useUIStore } from '@/stores/uiStore'
import SceneRenderer from '@/components/scene/SceneRenderer.vue'
import DashboardOverlay from '@/components/dashboard/DashboardOverlay.vue'

const { connect } = useWebSocket()
const simulationStore = useSimulationStore()
const uiStore = useUIStore()

const connectionStatus = computed(() => simulationStore.connectionStatus)
const sceneLoading = computed(() => uiStore.sceneLoadStatus === 'loading')
const sceneError = computed(() => uiStore.sceneLoadStatus === 'error')

function getWsUrl(): string {
  const proto = window.location.protocol === 'https:' ? 'wss' : 'ws'
  return `${proto}://${window.location.host}/ws/simulation`
}

function handleConnect() {
  connect(getWsUrl())
}

onMounted(() => {
  connect(getWsUrl())
})
</script>

<template>
  <div class="app-root">
    <SceneRenderer />
    <div class="app-atmosphere" />
    <DashboardOverlay />

    <div v-if="sceneLoading" class="status-overlay showroom-card">
      <div class="loading-spinner" />
      <p class="status-text">正在加载展厅场景</p>
      <p class="status-sub">首次打开会先解码模型和材质。</p>
    </div>

    <div v-if="sceneError" class="status-overlay showroom-card">
      <p class="status-text">3D 场景加载失败</p>
      <p class="status-sub">请检查模型与纹理资源是否可用。</p>
    </div>

    <div v-if="!sceneLoading && connectionStatus !== 'connected'" class="connection-fallback showroom-card">
      <p class="status-text">实时连接暂时不可用</p>
      <button class="retry-btn" @click="handleConnect">重新连接</button>
    </div>
  </div>
</template>

<style scoped>
.app-root {
  width: 100vw;
  height: 100vh;
  position: relative;
  overflow: hidden;
  background:
    radial-gradient(circle at 78% 44%, rgba(255, 231, 74, 0.1), transparent 24%),
    radial-gradient(circle at 18% 12%, rgba(115, 142, 164, 0.18), transparent 30%),
    linear-gradient(180deg, #090b10 0%, #06080c 100%);
}

.app-atmosphere {
  position: absolute;
  inset: 0;
  pointer-events: none;
  background:
    radial-gradient(circle at 72% 58%, rgba(255, 255, 255, 0.05), transparent 22%),
    linear-gradient(180deg, rgba(255, 255, 255, 0.02), transparent 20%, transparent 80%, rgba(0, 0, 0, 0.28));
  z-index: 2;
}

.status-overlay,
.connection-fallback {
  position: absolute;
  left: 50%;
  transform: translateX(-50%);
  z-index: var(--z-modal);
}

.status-overlay {
  top: 50%;
  transform: translate(-50%, -50%);
  text-align: center;
  min-width: 280px;
}

.connection-fallback {
  top: 18px;
  padding: 12px 16px;
  display: flex;
  align-items: center;
  gap: 14px;
}

.status-text,
.status-sub {
  margin: 0;
}

.status-text {
  font-size: 15px;
  color: var(--color-text-primary);
}

.status-sub {
  margin-top: 6px;
  font-size: 12px;
  color: var(--color-text-secondary);
}

.loading-spinner {
  width: 34px;
  height: 34px;
  border: 2px solid rgba(255, 255, 255, 0.08);
  border-top-color: var(--color-primary);
  border-radius: 50%;
  margin: 0 auto 14px;
  animation: spin 1s linear infinite;
}

.retry-btn {
  min-height: 34px;
  padding: 0 14px;
  border: 1px solid var(--color-border);
  border-radius: 999px;
  background: rgba(255, 255, 255, 0.03);
  color: var(--color-text-secondary);
  cursor: pointer;
}

@keyframes spin {
  to {
    transform: rotate(360deg);
  }
}
</style>
