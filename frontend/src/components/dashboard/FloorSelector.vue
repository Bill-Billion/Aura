<script setup lang="ts">
import { useUIStore } from '@/stores/uiStore'

const uiStore = useUIStore()

const floors = [
  { id: 'F3', label: 'F3', detail: '' },
  { id: 'F2', label: 'F2', detail: '' },
  { id: 'F1', label: 'F1', detail: '' },
  { id: 'overview', label: 'All', detail: '' },
]

function selectFloor(floorId: string) {
  uiStore.setActiveFloor(floorId)
  if (floorId === 'overview') {
    uiStore.setActiveDevice(null)
  }
}
</script>

<template>
  <nav class="floor-selector">
    <button
      v-for="floor in floors"
      :key="floor.id"
      class="floor-btn"
      :class="{ active: uiStore.activeFloor === floor.id }"
      @click="selectFloor(floor.id)"
    >
      <span class="floor-btn__index">{{ floor.label }}</span>
      <span v-if="floor.detail" class="floor-btn__detail">{{ floor.detail }}</span>
    </button>
  </nav>
</template>

<style scoped>
.floor-selector {
  display: flex;
  flex-direction: column;
  justify-content: center;
  gap: 2px;
  width: 60px;
}

.floor-btn {
  position: relative;
  width: 60px;
  height: 94px;
  display: flex;
  align-items: center;
  justify-content: center;
  border: 0;
  background: transparent;
  color: rgba(244, 246, 248, 0.72);
  cursor: pointer;
  transition: color var(--transition-fast), transform var(--transition-fast);
}

.floor-btn:hover {
  color: rgba(244, 246, 248, 0.92);
}

.floor-btn.active {
  color: var(--color-primary);
}

.floor-btn.active::before {
  content: '';
  position: absolute;
  left: -6px;
  width: 2px;
  height: 36px;
  background: var(--color-primary);
  box-shadow: 0 0 10px rgba(255, 231, 74, 0.24);
}

.floor-btn__index {
  font-size: 18px;
  line-height: 1;
  font-weight: 500;
  letter-spacing: -0.04em;
}

.floor-btn__detail {
  display: none;
}

@media (max-width: 820px) {
  .floor-selector {
    flex-direction: row-reverse;
    width: auto;
    gap: 8px;
  }

  .floor-btn {
    width: 52px;
    height: 52px;
  }

  .floor-btn.active::before {
    left: 13px;
    bottom: -2px;
    width: 26px;
    height: 2px;
  }
}
</style>
