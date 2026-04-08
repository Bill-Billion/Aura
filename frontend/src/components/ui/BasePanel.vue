<script setup lang="ts">
defineProps<{
  label: string
  value: string | number
  unit?: string
  active?: boolean
  icon?: string
}>()
</script>

<template>
  <div class="base-panel" :class="{ active }">
    <div class="indicator" />
    <div class="content">
      <span v-if="icon" class="icon">{{ icon }}</span>
      <div class="info">
        <span class="label">{{ label }}</span>
        <span class="value">{{ value }}<span v-if="unit" class="unit">{{ unit }}</span></span>
      </div>
    </div>
  </div>
</template>

<style scoped>
.base-panel {
  display: flex;
  align-items: stretch;
  min-width: 56px;
  min-height: 45px;
  background: rgba(67, 66, 66, 0.8);
  backdrop-filter: var(--panel-blur);
  -webkit-backdrop-filter: var(--panel-blur);
  border-radius: 6px;
  overflow: hidden;
  transition: background var(--transition-normal);
  cursor: default;
}

.base-panel:hover {
  background: rgba(80, 80, 80, 0.85);
}

.base-panel.active {
  background: rgba(255, 255, 255, 0.1);
}

.indicator {
  width: 2px;
  flex-shrink: 0;
  background: var(--color-indicator-inactive);
  transition: background var(--transition-normal);
}

.base-panel.active .indicator {
  background: var(--color-indicator-active);
}

.content {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 10px;
  min-width: 0;
}

.icon {
  font-size: 16px;
  flex-shrink: 0;
}

.info {
  display: flex;
  flex-direction: column;
  gap: 1px;
  min-width: 0;
}

.label {
  font-size: 10px;
  color: var(--color-text-secondary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.value {
  font-size: 14px;
  font-weight: 600;
  color: var(--color-text-primary);
  white-space: nowrap;
}

.unit {
  font-size: 11px;
  font-weight: 400;
  color: var(--color-text-secondary);
  margin-left: 2px;
}
</style>
