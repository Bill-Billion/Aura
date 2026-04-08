<script setup lang="ts">
import { onMounted, computed } from 'vue'
import { useWebSocket } from '@/composables/useWebSocket'
import { useSimulationStore } from '@/stores/simulationStore'
import SceneRenderer from '@/components/scene/SceneRenderer.vue'
import DashboardOverlay from '@/components/dashboard/DashboardOverlay.vue'

const { connect } = useWebSocket()
const simulationStore = useSimulationStore()

const disconnected = computed(() => simulationStore.connectionStatus === 'disconnected')

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
    <div v-if="disconnected" class="connect-overlay glass-panel">
      <p class="connect-text">未连接到仿真服务器</p>
      <button class="connect-btn" @click="handleConnect">
        连接
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

.connect-overlay {
  position: absolute;
  top: 50%;
  left: 50%;
  transform: translate(-50%, -50%);
  padding: 32px 48px;
  text-align: center;
  z-index: var(--z-modal);
}

.connect-text {
  font-size: 16px;
  color: var(--color-text-secondary);
  margin: 0 0 16px 0;
}

.connect-btn {
  padding: 8px 24px;
  border: 1px solid rgba(255, 231, 74, 0.4);
  border-radius: 8px;
  background: rgba(255, 231, 74, 0.12);
  color: var(--color-primary);
  font-size: 14px;
  font-weight: 500;
  cursor: pointer;
  transition: all var(--transition-normal);
}

.connect-btn:hover {
  background: rgba(255, 231, 74, 0.25);
}
</style>
