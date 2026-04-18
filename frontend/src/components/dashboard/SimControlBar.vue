<script setup lang="ts">
import { computed } from 'vue'
import { useSimulationStore } from '@/stores/simulationStore'
import { useWebSocket } from '@/composables/useWebSocket'

const simulationStore = useSimulationStore()
const { sendCommand } = useWebSocket()

const isRunning = computed(() => simulationStore.isRunning)
const currentSpeed = computed(() => simulationStore.speed)
const speedOptions = [0.5, 1, 2, 5]

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
  <section class="sim-control showroom-card">
    <div class="sim-control__group">
      <button class="sim-btn" :class="{ active: isRunning }" :disabled="isRunning" @click="startSimulation">开始</button>
      <button class="sim-btn" :disabled="!isRunning" @click="pauseSimulation">暂停</button>
      <button class="sim-btn" @click="resetSimulation">重置</button>
    </div>
    <div class="sim-control__group sim-control__group--speed">
      <button
        v-for="sp in speedOptions"
        :key="sp"
        class="sim-btn sim-btn--speed"
        :class="{ active: currentSpeed === sp }"
        @click="setSpeed(sp)"
      >
        {{ sp }}x
      </button>
    </div>
  </section>
</template>

<style scoped>
.sim-control {
  display: inline-flex;
  align-items: center;
  gap: 10px;
  padding: 6px 10px;
}

.sim-control__group {
  display: flex;
  gap: 6px;
}

.sim-btn {
  min-height: 32px;
  padding: 0 12px;
  border: 1px solid var(--color-border);
  border-radius: 999px;
  background: rgba(255, 255, 255, 0.02);
  color: var(--color-text-secondary);
  cursor: pointer;
  transition: all var(--transition-fast);
}

.sim-btn:hover:not(:disabled) {
  border-color: rgba(255, 255, 255, 0.16);
  color: var(--color-text-primary);
}

.sim-btn:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}

.sim-btn.active {
  border-color: rgba(255, 231, 74, 0.5);
  background: rgba(255, 231, 74, 0.08);
  color: var(--color-primary);
}

.sim-btn:active:not(:disabled) {
  transform: scale(0.95);
}
</style>
