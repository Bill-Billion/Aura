<script setup lang="ts">
import { computed } from 'vue'
import { useWebSocket } from '@/composables/useWebSocket'
import DeviceButton from '@/components/ui/DeviceButton.vue'
import NumberStepper from '@/components/ui/NumberStepper.vue'
import ModeSelector from '@/components/ui/ModeSelector.vue'
import LevelSelector from '@/components/ui/LevelSelector.vue'
import type { DeviceState } from '@/types/world-state'

const props = defineProps<{
  deviceId: string
  device: DeviceState
}>()

const { sendCommand } = useWebSocket()
const isPowered = computed(() => props.device.state.power)
const targetTemp = computed(() => props.device.state.extra.target_temp ?? 24)
const mode = computed(() => props.device.state.extra.mode ?? 'cool')
const speed = computed(() => props.device.state.extra.speed ?? 'medium')

const modes = [
  { value: 'auto', label: '自动' },
  { value: 'cool', label: '制冷' },
  { value: 'heat', label: '制热' },
  { value: 'fan', label: '送风' },
  { value: 'dry', label: '除湿' },
]

const speedOptions = [
  { value: 'low', label: '低速' },
  { value: 'medium', label: '中速' },
  { value: 'high', label: '高速' },
]

function togglePower() {
  sendCommand('CMD_DEVICE_CONTROL', {
    device_id: props.deviceId,
    action: isPowered.value ? 'turn_off' : 'turn_on',
  })
}

function setTargetTemp(value: number) {
  sendCommand('CMD_DEVICE_CONTROL', {
    device_id: props.deviceId,
    action: 'set_state',
    params: { target_temp: value },
  })
}

function setMode(value: string) {
  sendCommand('CMD_DEVICE_CONTROL', {
    device_id: props.deviceId,
    action: 'set_state',
    params: { mode: value },
  })
}

function setSpeed(value: number | string) {
  sendCommand('CMD_DEVICE_CONTROL', {
    device_id: props.deviceId,
    action: 'set_state',
    params: { speed: value },
  })
}
</script>

<template>
  <div class="device-panel glass-panel">
    <div class="device-panel__top">
      <div>
        <p class="device-panel__name">{{ device.display_name || deviceId }}</p>
        <p class="device-panel__status">{{ isPowered ? `${targetTemp}°C · ${mode} · ${speed}` : '已关闭' }}</p>
      </div>
      <DeviceButton :active="isPowered" :label="isPowered ? 'ON' : 'OFF'" @click="togglePower" />
    </div>

    <div class="device-panel__body" :class="{ disabled: !isPowered }">
      <div class="device-panel__stepper">
        <NumberStepper
          :model-value="targetTemp"
          :min="16"
          :max="30"
          :step="1"
          unit="°C"
          :disabled="!isPowered"
          @update:model-value="setTargetTemp"
        />
      </div>
      <ModeSelector
        label="模式"
        :model-value="mode"
        :modes="modes"
        :disabled="!isPowered"
        @update:model-value="setMode"
      />
      <LevelSelector
        label="风速"
        :model-value="speed"
        :options="speedOptions"
        :disabled="!isPowered"
        @update:model-value="setSpeed"
      />
    </div>
  </div>
</template>

<style scoped>
.device-panel {
  padding: 14px;
}

.device-panel__top {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
}

.device-panel__name,
.device-panel__status {
  margin: 0;
}

.device-panel__name {
  font-size: 13px;
}

.device-panel__status {
  margin-top: 4px;
  font-size: 11px;
  color: var(--color-text-secondary);
}

.device-panel__body {
  display: flex;
  flex-direction: column;
  gap: 12px;
  margin-top: 14px;
}

.device-panel__body.disabled {
  opacity: 0.38;
  pointer-events: none;
}

.device-panel__stepper {
  display: flex;
  justify-content: center;
}
</style>
