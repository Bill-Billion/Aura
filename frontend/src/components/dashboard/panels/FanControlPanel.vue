<script setup lang="ts">
import { computed } from 'vue'
import { useWebSocket } from '@/composables/useWebSocket'
import DeviceButton from '@/components/ui/DeviceButton.vue'
import LevelSelector from '@/components/ui/LevelSelector.vue'
import NumberStepper from '@/components/ui/NumberStepper.vue'
import type { DeviceState } from '@/types/world-state'

const props = defineProps<{
  deviceId: string
  device: DeviceState
}>()

const { sendCommand } = useWebSocket()

const isPowered = computed(() => props.device.state.power)
const speed = computed(() => props.device.state.extra.speed ?? 'low')
const shake = computed(() => Boolean(props.device.state.extra.shake))
const timeout = computed(() => Number(props.device.state.extra.timeout ?? 0))

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

function setSpeed(value: number | string) {
  sendCommand('CMD_DEVICE_CONTROL', {
    device_id: props.deviceId,
    action: 'set_state',
    params: { speed: value },
  })
}

function toggleShake() {
  sendCommand('CMD_DEVICE_CONTROL', {
    device_id: props.deviceId,
    action: 'set_state',
    params: { shake: !shake.value },
  })
}

function setTimeoutMinutes(value: number) {
  sendCommand('CMD_DEVICE_CONTROL', {
    device_id: props.deviceId,
    action: 'set_state',
    params: { timeout: value },
  })
}
</script>

<template>
  <div class="device-panel glass-panel">
    <div class="device-panel__top">
      <div>
        <p class="device-panel__name">{{ device.display_name || deviceId }}</p>
        <p class="device-panel__status">
          {{ isPowered ? `${speed} · ${shake ? '摇头中' : '固定送风'}` : '已关闭' }}
        </p>
      </div>
      <DeviceButton :active="isPowered" :label="isPowered ? 'ON' : 'OFF'" @click="togglePower" />
    </div>

    <div class="device-panel__body" :class="{ disabled: !isPowered }">
      <LevelSelector
        label="风速"
        :model-value="speed"
        :options="speedOptions"
        :disabled="!isPowered"
        @update:model-value="setSpeed"
      />

      <div class="device-panel__row">
        <span>摇头</span>
        <DeviceButton :active="shake" :label="shake ? '开启' : '关闭'" :disabled="!isPowered" @click="toggleShake" />
      </div>

      <div class="device-panel__timeout">
        <span class="device-panel__label">定时</span>
        <NumberStepper
          :model-value="timeout"
          :min="0"
          :max="120"
          :step="15"
          unit="min"
          :disabled="!isPowered"
          @update:model-value="setTimeoutMinutes"
        />
      </div>
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

.device-panel__status,
.device-panel__label {
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

.device-panel__timeout {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}
</style>
