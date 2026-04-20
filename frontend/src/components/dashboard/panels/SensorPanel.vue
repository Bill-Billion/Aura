<script setup lang="ts">
import { computed } from 'vue'
import type { DeviceState } from '@/types/world-state'

const props = defineProps<{
  deviceId: string
  device: DeviceState
}>()

const sensorType = computed(() => props.device.state.extra.sensor_type ?? 'sensor')
const value = computed(() => props.device.state.extra.value ?? '--')
const unit = computed(() => props.device.state.extra.unit ?? '')

const sensorLabel = computed(() => {
  const labels: Record<string, string> = {
    temperature: '温度',
    humidity: '湿度',
    light: '照度',
    air_quality: '空气质量',
  }
  return labels[sensorType.value] ?? sensorType.value
})
</script>

<template>
  <div class="device-panel glass-panel">
    <div class="device-panel__top">
      <div>
        <p class="device-panel__name">{{ device.display_name || deviceId }}</p>
        <p class="device-panel__status">只读环境输入</p>
      </div>
      <span class="device-panel__pill">READ</span>
    </div>

    <div class="sensor-reading">
      <span class="sensor-reading__label">{{ sensorLabel }}</span>
      <div class="sensor-reading__value">
        <span>{{ value }}</span>
        <span class="sensor-reading__unit">{{ unit }}</span>
      </div>
    </div>

    <p class="device-panel__hint">传感器默认只展示读数，不接受设备控制命令。</p>
  </div>
</template>

<style scoped>
.device-panel {
  padding: 14px;
}

.device-panel__top {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 10px;
}

.device-panel__name,
.device-panel__status,
.device-panel__hint,
.sensor-reading__label {
  margin: 0;
}

.device-panel__name {
  font-size: 13px;
  color: var(--color-text-primary);
}

.device-panel__status,
.device-panel__hint,
.sensor-reading__label,
.sensor-reading__unit {
  margin-top: 4px;
  font-size: 11px;
  color: var(--color-text-secondary);
}

.device-panel__pill {
  min-width: 60px;
  min-height: 28px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border-radius: 999px;
  border: 1px solid rgba(255, 255, 255, 0.12);
  background: rgba(255, 255, 255, 0.04);
  color: var(--color-text-secondary);
  font-size: 11px;
  letter-spacing: 0.12em;
}

.sensor-reading {
  margin-top: 14px;
  padding: 14px;
  border: 1px solid rgba(255, 255, 255, 0.08);
  background: rgba(255, 255, 255, 0.02);
}

.sensor-reading__value {
  display: flex;
  align-items: baseline;
  gap: 6px;
  margin-top: 10px;
  font-size: 34px;
  line-height: 1;
  letter-spacing: -0.05em;
  color: var(--color-primary);
}
</style>
