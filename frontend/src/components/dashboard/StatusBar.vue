<script setup lang="ts">
import { computed } from 'vue'
import { useWorldStore } from '@/stores/worldStore'
import { useSimulationStore } from '@/stores/simulationStore'

const worldStore = useWorldStore()
const simulationStore = useSimulationStore()

const timeOfDay = computed(() => worldStore.environment.time_of_day)
const outdoorTemp = computed(() => worldStore.environment.outdoor_temp)
const tickCount = computed(() => worldStore.simulationTick)
const isRunning = computed(() => simulationStore.isRunning)

const statusDotClass = computed(() => {
  return isRunning.value ? 'status-running' : 'status-paused'
})

const statusText = computed(() => {
  return isRunning.value ? '运行中' : '已暂停'
})
</script>

<template>
  <div class="status-bar glass-panel">
    <div class="status-item">
      <span class="status-icon">&#9200;</span>
      <span class="status-value">{{ timeOfDay }}</span>
    </div>
    <div class="status-divider" />
    <div class="status-item">
      <span class="status-icon">&#127777;&#65039;</span>
      <span class="status-value">{{ outdoorTemp }}°C</span>
    </div>
    <div class="status-divider" />
    <div class="status-item">
      <span class="status-dot" :class="statusDotClass" />
      <span class="status-value">{{ statusText }}</span>
    </div>
    <div class="status-divider" />
    <div class="status-item">
      <span class="status-icon">&#9201;</span>
      <span class="status-value">Tick: {{ tickCount }}</span>
    </div>
  </div>
</template>

<style scoped>
.status-bar {
  position: absolute;
  bottom: 16px;
  left: 50%;
  transform: translateX(-50%);
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 10px 24px;
  z-index: 20;
}

.status-item {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
  color: #c0c0d0;
  white-space: nowrap;
}

.status-icon {
  font-size: 14px;
}

.status-value {
  font-weight: 500;
}

.status-divider {
  width: 1px;
  height: 16px;
  background: rgba(255, 255, 255, 0.12);
}

.status-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  display: inline-block;
}

.status-dot.status-running {
  background: #4ade80;
  box-shadow: 0 0 6px rgba(74, 222, 128, 0.5);
}

.status-dot.status-paused {
  background: #facc15;
  box-shadow: 0 0 6px rgba(250, 204, 21, 0.5);
}
</style>
