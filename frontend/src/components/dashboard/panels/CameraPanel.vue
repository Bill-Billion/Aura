<script setup lang="ts">
import { computed } from 'vue'
import type { DeviceState } from '@/types/world-state'

const props = defineProps<{
  deviceId: string
  device: DeviceState
}>()

const isOnline = computed(() => Boolean(props.device.state.extra.online))
const previewLabel = computed(() => props.device.state.extra.preview_label ?? '实时画面')
const feedKey = computed(() => props.device.state.extra.feed_key ?? props.deviceId)
</script>

<template>
  <div class="device-panel glass-panel">
    <div class="device-panel__top">
      <div>
        <p class="device-panel__name">{{ device.display_name || deviceId }}</p>
        <p class="device-panel__status">{{ isOnline ? '在线' : '离线' }}</p>
      </div>
      <span class="device-panel__pill" :class="{ offline: !isOnline }">
        {{ isOnline ? 'LIVE' : 'OFFLINE' }}
      </span>
    </div>

    <div class="camera-preview" :class="{ offline: !isOnline }">
      <div class="camera-preview__scan" />
      <div class="camera-preview__body">
        <p class="camera-preview__title">{{ previewLabel }}</p>
        <p class="camera-preview__meta">源 {{ feedKey }}</p>
      </div>
    </div>

    <p class="device-panel__hint">当前版本先提供楼层归属、在线状态和画面入口，云台与录制控制暂不暴露。</p>
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
.camera-preview__title,
.camera-preview__meta {
  margin: 0;
}

.device-panel__name {
  font-size: 13px;
  color: var(--color-text-primary);
}

.device-panel__status,
.device-panel__hint,
.camera-preview__meta {
  margin-top: 4px;
  font-size: 11px;
  color: var(--color-text-secondary);
}

.device-panel__pill {
  min-width: 72px;
  min-height: 28px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border-radius: 999px;
  border: 1px solid rgba(255, 231, 74, 0.34);
  color: var(--color-primary);
  background: rgba(255, 231, 74, 0.08);
  font-size: 11px;
  letter-spacing: 0.12em;
}

.device-panel__pill.offline {
  border-color: rgba(255, 255, 255, 0.12);
  color: var(--color-text-secondary);
  background: rgba(255, 255, 255, 0.04);
}

.camera-preview {
  position: relative;
  overflow: hidden;
  min-height: 154px;
  margin-top: 14px;
  padding: 14px;
  border: 1px solid rgba(255, 255, 255, 0.08);
  background:
    linear-gradient(180deg, rgba(255, 255, 255, 0.03), rgba(255, 255, 255, 0.01)),
    linear-gradient(180deg, rgba(11, 15, 21, 0.98), rgba(22, 28, 38, 0.98));
}

.camera-preview.offline {
  opacity: 0.7;
}

.camera-preview__scan {
  position: absolute;
  inset: 0;
  background:
    radial-gradient(circle at 68% 28%, rgba(255, 231, 74, 0.14), transparent 18%),
    repeating-linear-gradient(180deg, rgba(255, 255, 255, 0.04) 0, rgba(255, 255, 255, 0.04) 1px, transparent 1px, transparent 22px);
}

.camera-preview__body {
  position: relative;
  z-index: 1;
  display: flex;
  flex-direction: column;
  justify-content: flex-end;
  min-height: 126px;
}

.camera-preview__title {
  font-size: 18px;
  color: var(--color-text-primary);
}
</style>
