<script setup lang="ts">
import { computed } from 'vue'
import { useSimulationStore } from '@/stores/simulationStore'
import { useWebSocket } from '@/composables/useWebSocket'

const simulationStore = useSimulationStore()
const { sendCommand } = useWebSocket()

const isRunning = computed(() => simulationStore.isRunning)
const currentSpeed = computed(() => simulationStore.speed)

const speedOptions = [0.5, 1, 2, 5, 10]

function startSimulation() {
  sendCommand('CMD_SIM_START')
}

function pauseSimulation() {
  sendCommand('CMD_SIM_PAUSE')
}

function resetSimulation() {
  sendCommand('CMD_SIM_RESET')
}

function setSpeed(speed: number) {
  simulationStore.setSpeed(speed)
  sendCommand('CMD_SIM_SPEED', { speed })
}
</script>

<template>
  <div class="sim-control glass-panel">
    <div class="ctrl-buttons">
      <button
        class="ctrl-btn"
        :class="{ active: isRunning }"
        :disabled="isRunning"
        title="开始"
        @click="startSimulation"
      >
        <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><polygon points="5 3 19 12 5 21 5 3" /></svg>
      </button>
      <button
        class="ctrl-btn"
        :disabled="!isRunning"
        title="暂停"
        @click="pauseSimulation"
      >
        <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="4" width="4" height="16" /><rect x="14" y="4" width="4" height="16" /></svg>
      </button>
      <button
        class="ctrl-btn"
        title="重置"
        @click="resetSimulation"
      >
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="23 4 23 10 17 10" /><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10" /></svg>
      </button>
    </div>
    <div class="speed-selector">
      <button
        v-for="sp in speedOptions"
        :key="sp"
        class="speed-btn"
        :class="{ active: currentSpeed === sp }"
        @click="setSpeed(sp)"
      >
        {{ sp }}x
      </button>
    </div>
  </div>
</template>

<style scoped>
.sim-control {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 12px;
}

.ctrl-buttons {
  display: flex;
  gap: 4px;
}

.ctrl-btn {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 32px;
  height: 32px;
  border: 1px solid var(--color-border);
  border-radius: 6px;
  background: transparent;
  color: var(--color-text-secondary);
  cursor: pointer;
  transition: all var(--transition-fast);
}

.ctrl-btn:hover:not(:disabled) {
  color: var(--color-text-primary);
  border-color: var(--color-border-strong);
  background: rgba(255, 255, 255, 0.05);
}

.ctrl-btn:disabled {
  opacity: 0.3;
  cursor: not-allowed;
}

.ctrl-btn.active {
  color: var(--color-primary);
  border-color: rgba(255, 231, 74, 0.3);
}

.speed-selector {
  display: flex;
  gap: 2px;
  background: rgba(255, 255, 255, 0.04);
  border-radius: 6px;
  padding: 2px;
}

.speed-btn {
  padding: 4px 8px;
  border: none;
  border-radius: 4px;
  background: transparent;
  color: var(--color-text-muted);
  font-size: 10px;
  font-weight: 500;
  cursor: pointer;
  transition: all var(--transition-fast);
}

.speed-btn:hover:not(.active) {
  color: var(--color-text-secondary);
}

.speed-btn.active {
  background: var(--color-primary);
  color: #000;
  font-weight: 700;
}
</style>
