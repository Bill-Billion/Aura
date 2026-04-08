<script setup lang="ts">
import { useUIStore } from '@/stores/uiStore'

const uiStore = useUIStore()

const floors = [
  { id: 'overview', label: '总览', icon: '🏠' },
  { id: 'F1', label: '1F', icon: '1' },
  { id: 'F2', label: '2F', icon: '2' },
  { id: 'F3', label: '3F', icon: '3' },
]

function selectFloor(floorId: string) {
  uiStore.setActiveFloor(floorId)
}
</script>

<template>
  <div class="floor-selector">
    <button
      v-for="floor in floors"
      :key="floor.id"
      class="floor-btn"
      :class="{ active: uiStore.activeFloor === floor.id }"
      @click="selectFloor(floor.id)"
    >
      <div class="floor-indicator" />
      <div class="floor-content">
        <span class="floor-icon">{{ floor.icon }}</span>
        <span class="floor-label">{{ floor.label }}</span>
      </div>
    </button>
  </div>
</template>

<style scoped>
.floor-selector {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.floor-btn {
  display: flex;
  align-items: center;
  width: 60px;
  height: 60px;
  border: none;
  background: transparent;
  cursor: pointer;
  position: relative;
  transition: all var(--transition-normal);
}

.floor-btn:hover {
  transform: scale(1.1);
}

.floor-indicator {
  position: absolute;
  left: 0;
  top: 50%;
  transform: translateY(-50%);
  width: 2px;
  height: 0;
  background: var(--color-primary);
  border-radius: 1px;
  transition: height var(--transition-normal);
}

.floor-btn.active .floor-indicator {
  height: 30px;
}

.floor-content {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  width: 100%;
  height: 100%;
  gap: 2px;
  background: var(--panel-bg);
  backdrop-filter: var(--panel-blur);
  -webkit-backdrop-filter: var(--panel-blur);
  border: var(--panel-border);
  border-radius: var(--panel-radius);
  margin-left: 6px;
  transition: all var(--transition-normal);
}

.floor-btn.active .floor-content {
  background: rgba(255, 255, 255, 0.1);
  border-color: rgba(255, 231, 74, 0.3);
}

.floor-icon {
  font-size: 16px;
  font-weight: 700;
  color: var(--color-text-secondary);
  line-height: 1;
}

.floor-btn.active .floor-icon {
  color: var(--color-primary);
}

.floor-label {
  font-size: 9px;
  color: var(--color-text-secondary);
  transition: color var(--transition-normal);
}

.floor-btn.active .floor-label {
  color: var(--color-primary);
  font-weight: 600;
}
</style>
