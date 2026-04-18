<script setup lang="ts">
import { computed } from 'vue'
import { useWebSocket } from '@/composables/useWebSocket'
import DeviceButton from '@/components/ui/DeviceButton.vue'
import type { DeviceState } from '@/types/world-state'

const props = defineProps<{
  deviceId: string
  device: DeviceState
}>()

const { sendCommand } = useWebSocket()
const openPercent = computed(() => props.device.state.extra.open_percent ?? 0)

function setOpenPercent(value: number) {
  sendCommand('CMD_DEVICE_CONTROL', {
    device_id: props.deviceId,
    action: 'set_state',
    params: { open_percent: value },
  })
}

function onSliderInput(event: Event) {
  setOpenPercent(+(event.target as HTMLInputElement).value)
}
</script>

<template>
  <div class="device-panel glass-panel">
    <div class="device-panel__top">
      <div>
        <p class="device-panel__name">{{ deviceId }}</p>
        <p class="device-panel__status">{{ openPercent }}% 开启</p>
      </div>
    </div>

    <div class="device-panel__body">
      <div class="device-panel__row">
        <span>开合度</span>
        <span>{{ openPercent }}%</span>
      </div>
      <input type="range" min="0" max="100" :value="openPercent" @input="onSliderInput" />

      <div class="device-panel__actions">
        <DeviceButton :active="openPercent >= 100" label="全开" @click="setOpenPercent(100)" />
        <DeviceButton :active="openPercent <= 0" label="全关" @click="setOpenPercent(0)" />
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

.device-panel__row {
  font-size: 11px;
  color: var(--color-text-secondary);
}

.device-panel__actions {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 8px;
}
</style>
