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
  const value = +(event.target as HTMLInputElement).value
  setOpenPercent(value)
}

const deviceLabel = computed(() => {
  const parts = props.deviceId.split('_')
  return parts.length > 1 ? `窗帘 ${parts[parts.length - 1]}` : props.deviceId
})
</script>

<template>
  <div class="curtain-panel glass-panel">
    <div class="panel-top">
      <div class="device-info">
        <div class="indicator" :class="{ active: openPercent > 0 }" />
        <div class="info-text">
          <span class="device-name">{{ deviceLabel }}</span>
          <span class="device-status">{{ openPercent }}% 开启</span>
        </div>
      </div>
    </div>

    <div class="panel-body">
      <div class="control-row">
        <span class="control-label">开合度</span>
        <span class="control-value">{{ openPercent }}%</span>
      </div>
      <input
        type="range"
        min="0"
        max="100"
        :value="openPercent"
        @input="onSliderInput"
      />

      <div class="quick-actions">
        <DeviceButton
          :active="openPercent >= 100"
          label="全开"
          @click="setOpenPercent(100)"
        />
        <DeviceButton
          :active="openPercent <= 0"
          label="全关"
          @click="setOpenPercent(0)"
        />
      </div>
    </div>
  </div>
</template>

<style scoped>
.curtain-panel {
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

.quick-actions {
  display: flex;
  gap: 8px;
}

.quick-actions > * {
  flex: 1;
}
</style>
