<script setup lang="ts">
import { computed } from 'vue'
import { useWebSocket } from '@/composables/useWebSocket'
import DeviceButton from '@/components/ui/DeviceButton.vue'
import LevelSelector from '@/components/ui/LevelSelector.vue'
import type { DeviceState } from '@/types/world-state'

const props = defineProps<{
  deviceId: string
  device: DeviceState
}>()

const { sendCommand } = useWebSocket()

const isPowered = computed(() => props.device.state.power)
const brightness = computed(() => props.device.state.extra.brightness ?? 0)

const colorTempOptions = [
  { value: 2700, label: '暖光' },
  { value: 4000, label: '自然' },
  { value: 5000, label: '冷光' },
]

const currentColorTemp = computed(() => props.device.state.extra.color_temp ?? 4000)

function togglePower() {
  sendCommand('CMD_DEVICE_CONTROL', {
    device_id: props.deviceId,
    action: isPowered.value ? 'turn_off' : 'turn_on',
  })
}

function setBrightness(event: Event) {
  const value = +(event.target as HTMLInputElement).value
  sendCommand('CMD_DEVICE_CONTROL', {
    device_id: props.deviceId,
    action: 'set_state',
    params: { brightness: value },
  })
}

function setColorTemp(value: number | string) {
  sendCommand('CMD_DEVICE_CONTROL', {
    device_id: props.deviceId,
    action: 'set_state',
    params: { color_temp: Number(value) },
  })
}

const deviceLabel = computed(() => {
  const parts = props.deviceId.split('_')
  return parts.length > 1 ? `灯光 ${parts[parts.length - 1]}` : props.deviceId
})
</script>

<template>
  <div class="light-panel glass-panel">
    <div class="panel-top">
      <div class="device-info">
        <div class="indicator" :class="{ active: isPowered }" />
        <div class="info-text">
          <span class="device-name">{{ deviceLabel }}</span>
          <span class="device-status">{{ isPowered ? `${brightness}%` : '关闭' }}</span>
        </div>
      </div>
      <DeviceButton
        :active="isPowered"
        :label="isPowered ? 'ON' : 'OFF'"
        @click="togglePower"
      />
    </div>

    <div class="panel-body" :class="{ disabled: !isPowered }">
      <div class="control-row">
        <span class="control-label">亮度</span>
        <span class="control-value">{{ brightness }}%</span>
      </div>
      <input
        type="range"
        min="0"
        max="100"
        :value="brightness"
        :disabled="!isPowered"
        @input="setBrightness"
      />

      <LevelSelector
        label="色温"
        :model-value="currentColorTemp"
        :options="colorTempOptions"
        :disabled="!isPowered"
        @update:model-value="setColorTemp"
      />
    </div>
  </div>
</template>

<style scoped>
.light-panel {
  padding: 12px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.panel-top {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.device-info {
  display: flex;
  align-items: center;
  gap: 8px;
}

.indicator {
  width: 3px;
  height: 28px;
  border-radius: 2px;
  background: var(--color-indicator-inactive);
  transition: background var(--transition-normal);
}

.indicator.active {
  background: var(--color-indicator-active);
  box-shadow: 0 0 8px rgba(255, 231, 74, 0.4);
}

.info-text {
  display: flex;
  flex-direction: column;
  gap: 1px;
}

.device-name {
  font-size: 13px;
  font-weight: 600;
  color: var(--color-text-primary);
}

.device-status {
  font-size: 10px;
  color: var(--color-text-secondary);
}

.panel-body {
  display: flex;
  flex-direction: column;
  gap: 10px;
  transition: opacity var(--transition-normal);
}

.panel-body.disabled {
  opacity: 0.35;
  pointer-events: none;
}

.control-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.control-label {
  font-size: 10px;
  color: var(--color-text-secondary);
}

.control-value {
  font-size: 11px;
  font-weight: 600;
  color: var(--color-primary);
}
</style>
