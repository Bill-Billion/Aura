<script setup lang="ts">
import { computed } from 'vue'
import { useWebSocket } from '@/composables/useWebSocket'
import DeviceButton from '@/components/ui/DeviceButton.vue'
import NumberStepper from '@/components/ui/NumberStepper.vue'
import ModeSelector from '@/components/ui/ModeSelector.vue'
import type { DeviceState } from '@/types/world-state'

const props = defineProps<{
  deviceId: string
  device: DeviceState
}>()

const { sendCommand } = useWebSocket()

const isPowered = computed(() => props.device.state.power)
const targetTemp = computed(() => props.device.state.extra.target_temp ?? 24)
const mode = computed(() => props.device.state.extra.mode ?? 'cool')

const modes = [
  { value: 'cool', label: '制冷', icon: '❄' },
  { value: 'heat', label: '制热', icon: '🔥' },
  { value: 'auto', label: '自动', icon: '⚡' },
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

const deviceLabel = computed(() => {
  const parts = props.deviceId.split('_')
  return parts.length > 1 ? `空调 ${parts[parts.length - 1]}` : props.deviceId
})

const modeColor = computed(() => {
  if (!isPowered.value) return 'var(--color-text-muted)'
  return mode.value === 'heat' ? 'var(--color-heat)' : 'var(--color-cool)'
})
</script>

<template>
  <div class="hvac-panel glass-panel">
    <div class="panel-top">
      <div class="device-info">
        <div class="indicator" :class="{ active: isPowered }" :style="{ background: isPowered ? modeColor : undefined }" />
        <div class="info-text">
          <span class="device-name">{{ deviceLabel }}</span>
          <span class="device-status">{{ isPowered ? `${targetTemp}°C` : '关闭' }}</span>
        </div>
      </div>
      <DeviceButton
        :active="isPowered"
        :label="isPowered ? 'ON' : 'OFF'"
        @click="togglePower"
      />
    </div>

    <div class="panel-body" :class="{ disabled: !isPowered }">
      <div class="temp-row">
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
    </div>
  </div>
</template>

<style scoped>
.hvac-panel {
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
  box-shadow: 0 0 8px currentColor;
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
  gap: 12px;
  transition: opacity var(--transition-normal);
}

.panel-body.disabled {
  opacity: 0.35;
  pointer-events: none;
}

.temp-row {
  display: flex;
  justify-content: center;
}
</style>
