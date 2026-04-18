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
const currentColorTemp = computed(() => props.device.state.extra.color_temp ?? 4000)

const colorTempOptions = [
  { value: 2700, label: '暖光' },
  { value: 4000, label: '自然' },
  { value: 5000, label: '冷光' },
]

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
</script>

<template>
  <div class="device-panel glass-panel">
    <div class="device-panel__top">
      <div>
        <p class="device-panel__name">{{ deviceId }}</p>
        <p class="device-panel__status">{{ isPowered ? `${brightness}%` : '已关闭' }}</p>
      </div>
      <DeviceButton :active="isPowered" :label="isPowered ? 'ON' : 'OFF'" @click="togglePower" />
    </div>

    <div class="device-panel__body" :class="{ disabled: !isPowered }">
      <div class="device-panel__row">
        <span>亮度</span>
        <span>{{ brightness }}%</span>
      </div>
      <input type="range" min="0" max="100" :value="brightness" :disabled="!isPowered" @input="setBrightness" />
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
.device-panel {
  padding: 14px;
}

.device-panel__top,
.device-panel__row {
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
  color: var(--color-text-primary);
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

.device-panel__row {
  font-size: 11px;
  color: var(--color-text-secondary);
}
</style>
