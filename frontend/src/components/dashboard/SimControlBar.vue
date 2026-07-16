<script setup lang="ts">
import { computed } from 'vue'
import { useSimulationStore } from '@/stores/simulationStore'
import { useWebSocket } from '@/composables/useWebSocket'

const simulationStore = useSimulationStore()
const { sendCommand } = useWebSocket()

const isRunning = computed(() => simulationStore.isRunning)
const currentMode = computed(() => simulationStore.mode)
const simulatedDtSeconds = computed(() => simulationStore.simulatedDtSeconds)
const wallTickSeconds = computed(() => Math.max(1, simulationStore.wallTickMs / 1000))
const modeOptions = [
  { value: 'observe', label: '观察', desc: '每 2 秒推进 10 秒' },
  { value: 'demo', label: '演示', desc: '每 2 秒推进 60 秒' },
] as const
const pausedHint = computed(() => {
  const seconds = simulatedDtSeconds.value
  return `仿真未开始 · 当前${currentMode.value === 'observe' ? '观察' : '演示'}模式 · 启动后每 ${wallTickSeconds.value} 秒推进 ${seconds} 秒`
})

function startSimulation() {
  sendCommand('CMD_SIM_START')
}

function pauseSimulation() {
  sendCommand('CMD_SIM_PAUSE')
}

function resetSimulation() {
  sendCommand('CMD_SIM_RESET')
}

function setMode(mode: 'observe' | 'demo') {
  simulationStore.setMode(mode)
  sendCommand('CMD_SIM_MODE', { mode })
}
</script>

<template>
  <section class="sim-control showroom-card">
    <div class="sim-control__group">
      <button class="sim-btn" :class="{ active: isRunning }" :disabled="isRunning" @click="startSimulation">开始</button>
      <button class="sim-btn" :disabled="!isRunning" @click="pauseSimulation">暂停</button>
      <button class="sim-btn" @click="resetSimulation">重置</button>
    </div>
    <div class="sim-control__group sim-control__group--mode">
      <button
        v-for="item in modeOptions"
        :key="item.value"
        class="sim-btn sim-btn--mode"
        :class="{ active: currentMode === item.value }"
        @click="setMode(item.value)"
      >
        <span>{{ item.label }}</span>
        <small>{{ item.desc }}</small>
      </button>
    </div>
    <p v-if="!isRunning" class="sim-control__hint">{{ pausedHint }}</p>
  </section>
</template>

<style scoped>
.sim-control {
  display: inline-flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 10px 12px;
  padding: 6px 10px;
}

.sim-control__group {
  display: flex;
  gap: 6px;
}

.sim-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  min-height: 32px;
  padding: 0 12px;
  border: 1px solid var(--color-border);
  border-radius: 999px;
  background: rgba(255, 255, 255, 0.02);
  color: var(--color-text-secondary);
  cursor: pointer;
  transition: all var(--transition-fast);
}

.sim-btn small {
  font-size: 10px;
  color: var(--color-text-muted);
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

.sim-control__hint {
  margin: 0;
  font-size: 11px;
  color: var(--color-text-secondary);
}

.sim-btn:active:not(:disabled) {
  transform: scale(0.95);
}
</style>
