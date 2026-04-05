<script setup lang="ts">
import { computed } from 'vue'
import { useWorldStore } from '@/stores/worldStore'
import { useSimulationStore } from '@/stores/simulationStore'
import { useWebSocket } from '@/composables/useWebSocket'

const worldStore = useWorldStore()
const simulationStore = useSimulationStore()
const { sendCommand } = useWebSocket()

const isRunning = computed(() => simulationStore.isRunning)
const speed = computed({
  get: () => simulationStore.speed,
  set: (val: number) => {
    simulationStore.setSpeed(val)
    sendCommand('CMD_SIM_SPEED', { speed: val })
  },
})

const devices = computed(() => Object.values(worldStore.devices))

function startSimulation() {
  sendCommand('CMD_SIM_START')
}

function pauseSimulation() {
  sendCommand('CMD_SIM_PAUSE')
}

function resetSimulation() {
  sendCommand('CMD_SIM_RESET')
}

function toggleDevicePower(deviceId: string, currentPower: boolean) {
  sendCommand('CMD_DEVICE_CONTROL', {
    device_id: deviceId,
    action: currentPower ? 'turn_off' : 'turn_on',
  })
}

function setDeviceExtra(deviceId: string, key: string, value: number) {
  sendCommand('CMD_DEVICE_CONTROL', {
    device_id: deviceId,
    action: 'set_state',
    params: { [key]: value },
  })
}
</script>

<template>
  <div class="control-panel glass-panel">
    <!-- Simulation controls -->
    <div class="section">
      <h3 class="section-title">仿真控制</h3>
      <div class="btn-row">
        <button
          class="ctrl-btn"
          :disabled="isRunning"
          @click="startSimulation"
        >
          &#9654; 开始
        </button>
        <button
          class="ctrl-btn"
          :disabled="!isRunning"
          @click="pauseSimulation"
        >
          &#9208; 暂停
        </button>
        <button
          class="ctrl-btn"
          @click="resetSimulation"
        >
          &#8634; 重置
        </button>
      </div>

      <!-- Speed slider -->
      <div class="slider-group">
        <label class="slider-label">速度: {{ speed.toFixed(1) }}x</label>
        <input
          v-model.number="speed"
          type="range"
          min="0.5"
          max="10"
          step="0.5"
          class="slider"
        />
      </div>
    </div>

    <!-- Device controls -->
    <div class="section">
      <h3 class="section-title">设备控制</h3>
      <div class="device-list">
        <div v-for="dev in devices" :key="dev.id" class="device-item">
          <div class="device-header">
            <span class="device-name">{{ dev.id }}</span>
            <button
              class="power-btn"
              :class="{ active: dev.state.power }"
              @click="toggleDevicePower(dev.id, dev.state.power)"
            >
              {{ dev.state.power ? 'ON' : 'OFF' }}
            </button>
          </div>

          <!-- Light brightness -->
          <div v-if="dev.type === 'light'" class="device-control">
            <label class="control-label">亮度: {{ dev.state.extra.brightness ?? 0 }}%</label>
            <input
              type="range"
              min="0"
              max="100"
              :value="dev.state.extra.brightness ?? 0"
              class="slider slider-sm"
              @input="setDeviceExtra(dev.id, 'brightness', +($event.target as HTMLInputElement).value)"
            />
          </div>

          <!-- HVAC target temp -->
          <div v-if="dev.type === 'hvac'" class="device-control">
            <label class="control-label">目标温度: {{ dev.state.extra.target_temp ?? 22 }}°C</label>
            <input
              type="range"
              min="16"
              max="30"
              :value="dev.state.extra.target_temp ?? 22"
              class="slider slider-sm"
              @input="setDeviceExtra(dev.id, 'target_temp', +($event.target as HTMLInputElement).value)"
            />
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.control-panel {
  position: absolute;
  top: 16px;
  left: 16px;
  width: 280px;
  max-height: calc(100vh - 100px);
  overflow-y: auto;
  padding: 16px;
  z-index: 20;
}

.section {
  margin-bottom: 16px;
}

.section:last-child {
  margin-bottom: 0;
}

.section-title {
  font-size: 13px;
  font-weight: 600;
  color: #8a8aaa;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  margin: 0 0 10px 0;
}

.btn-row {
  display: flex;
  gap: 6px;
  margin-bottom: 12px;
}

.ctrl-btn {
  flex: 1;
  padding: 6px 8px;
  border: 1px solid rgba(255, 255, 255, 0.12);
  border-radius: 6px;
  background: rgba(255, 255, 255, 0.05);
  color: #c0c0d0;
  font-size: 12px;
  cursor: pointer;
  transition: all 0.2s;
}

.ctrl-btn:hover:not(:disabled) {
  background: rgba(255, 255, 255, 0.12);
}

.ctrl-btn:disabled {
  opacity: 0.35;
  cursor: not-allowed;
}

.slider-group {
  margin-bottom: 4px;
}

.slider-label,
.control-label {
  display: block;
  font-size: 11px;
  color: #8a8aaa;
  margin-bottom: 4px;
}

.slider {
  width: 100%;
  height: 4px;
  -webkit-appearance: none;
  appearance: none;
  background: rgba(255, 255, 255, 0.1);
  border-radius: 2px;
  outline: none;
}

.slider::-webkit-slider-thumb {
  -webkit-appearance: none;
  width: 14px;
  height: 14px;
  border-radius: 50%;
  background: #6366f1;
  cursor: pointer;
}

.slider.slider-sm {
  height: 3px;
}

.slider.slider-sm::-webkit-slider-thumb {
  width: 10px;
  height: 10px;
}

.device-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.device-item {
  padding: 8px;
  border: 1px solid rgba(255, 255, 255, 0.06);
  border-radius: 8px;
  background: rgba(255, 255, 255, 0.03);
}

.device-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 4px;
}

.device-name {
  font-size: 12px;
  font-weight: 500;
  color: #d0d0e0;
}

.power-btn {
  padding: 2px 8px;
  border: 1px solid rgba(255, 255, 255, 0.15);
  border-radius: 4px;
  background: rgba(255, 255, 255, 0.05);
  color: #888;
  font-size: 10px;
  font-weight: 600;
  cursor: pointer;
  transition: all 0.2s;
}

.power-btn.active {
  color: #4ade80;
  border-color: rgba(74, 222, 128, 0.3);
  background: rgba(74, 222, 128, 0.1);
}

.device-control {
  margin-top: 6px;
}

/* Scrollbar */
.control-panel::-webkit-scrollbar {
  width: 4px;
}

.control-panel::-webkit-scrollbar-track {
  background: transparent;
}

.control-panel::-webkit-scrollbar-thumb {
  background: rgba(255, 255, 255, 0.1);
  border-radius: 2px;
}
</style>
