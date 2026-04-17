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
const showOverlay = computed(() =>
  sceneLoading.value || connectionStatus.value === 'disconnected' || connectionStatus.value === 'error' || sceneError.value
)

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
    <DashboardOverlay />

    <!-- Loading overlay -->
    <div v-if="sceneLoading" class="status-overlay glass-panel">
      <div class="loading-spinner" />
      <p class="status-text">Loading scene...</p>
    </div>

    <!-- Scene load error -->
    <div v-if="sceneError" class="status-overlay glass-panel">
      <p class="status-text">Failed to load 3D scene</p>
      <p class="status-sub">Check console for details</p>
    </div>

    <!-- Connection status indicator (top-right corner) -->
    <div v-if="!sceneLoading && !sceneError" class="connection-indicator" :class="connectionStatus">
      <span class="conn-dot" />
      <span class="conn-label">
        {{ connectionStatus === 'connected' ? 'Connected' : connectionStatus === 'connecting' ? 'Connecting...' : 'Disconnected' }}
      </span>
      <button v-if="connectionStatus === 'disconnected' || connectionStatus === 'error'" class="retry-btn" @click="handleConnect">
        Retry
      </button>
    </div>
  </div>
</template>

<style scoped>
.app-root {
  width: 100vw;
  height: 100vh;
  position: relative;
  overflow: hidden;
  background: #000;
}

.status-overlay {
  position: absolute;
  top: 50%;
  left: 50%;
  transform: translate(-50%, -50%);
  padding: 32px 48px;
  text-align: center;
  z-index: var(--z-modal);
}

.status-text {
  font-size: 16px;
  color: var(--color-text-secondary);
  margin: 0 0 8px 0;
}

.status-sub {
  font-size: 13px;
  color: var(--color-text-tertiary, #666);
  margin: 0;
}

.loading-spinner {
  width: 32px;
  height: 32px;
  border: 2px solid rgba(255, 231, 74, 0.2);
  border-top-color: var(--color-primary, #ffe74a);
  border-radius: 50%;
  margin: 0 auto 16px;
  animation: spin 1s linear infinite;
}

@keyframes spin {
  to { transform: rotate(360deg); }
}

.connection-indicator {
  position: absolute;
  top: 12px;
  right: 12px;
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 4px 10px;
  border-radius: 12px;
  background: rgba(0, 0, 0, 0.5);
  backdrop-filter: blur(8px);
  font-size: 12px;
  z-index: var(--z-toolbar, 100);
  transition: all 0.3s ease;
}

.conn-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: #666;
  transition: background 0.3s;
}

.connection-indicator.connected .conn-dot {
  background: #4ade80;
}

.connection-indicator.connecting .conn-dot {
  background: #ffe74a;
  animation: pulse-dot 1.5s ease-in-out infinite;
}

.connection-indicator.error .conn-dot,
.connection-indicator.disconnected .conn-dot {
  background: #ef4444;
}

@keyframes pulse-dot {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.3; }
}

.conn-label {
  color: var(--color-text-secondary, #aaa);
}

.retry-btn {
  padding: 2px 8px;
  border: 1px solid rgba(255, 231, 74, 0.4);
  border-radius: 6px;
  background: rgba(255, 231, 74, 0.12);
  color: var(--color-primary, #ffe74a);
  font-size: 11px;
  cursor: pointer;
  transition: background 0.2s;
}

.retry-btn:hover {
  background: rgba(255, 231, 74, 0.25);
}
</style>
