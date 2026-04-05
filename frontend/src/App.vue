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
  return `${proto}://${window.location.host}/ws`
}

function handleConnect() {
  connect(getWsUrl())
}

onMounted(() => {
  connect(getWsUrl())
})
</script>

<template>
  <div class="w-screen h-screen relative overflow-hidden bg-[#0a0a0f]">
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
.connect-overlay {
  position: absolute;
  top: 50%;
  left: 50%;
  transform: translate(-50%, -50%);
  padding: 32px 48px;
  text-align: center;
  z-index: 50;
}

.connect-text {
  font-size: 16px;
  color: #c0c0d0;
  margin: 0 0 16px 0;
}

.connect-btn {
  padding: 8px 24px;
  border: 1px solid rgba(99, 102, 241, 0.4);
  border-radius: 8px;
  background: rgba(99, 102, 241, 0.15);
  color: #a5b4fc;
  font-size: 14px;
  cursor: pointer;
  transition: all 0.2s;
}

.connect-btn:hover {
  background: rgba(99, 102, 241, 0.3);
}
</style>
