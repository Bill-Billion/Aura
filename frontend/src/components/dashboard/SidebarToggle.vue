<script setup lang="ts">
import { useUIStore } from '@/stores/uiStore'

const uiStore = useUIStore()
</script>

<template>
  <button
    class="sidebar-toggle"
    :class="{ active: uiStore.sidebarOpen }"
    title="观测侧栏"
    @click="uiStore.toggleSidebar()"
  >
    <span class="sidebar-toggle__label">观测</span>
  </button>
</template>

<style scoped>
.sidebar-toggle {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 68px;
  height: 36px;
  padding: 0 12px;
  border: 1px solid rgba(255, 255, 255, 0.06);
  border-radius: 999px;
  background: rgba(255, 255, 255, 0.02);
  color: var(--color-text-muted);
  cursor: pointer;
  transition-property: border-color, color, background-color, transform;
  transition-duration: var(--transition-fast);
  transition-timing-function: ease;
}

.sidebar-toggle:hover {
  border-color: rgba(255, 255, 255, 0.14);
  color: var(--color-text-primary);
}

.sidebar-toggle.active {
  border-color: rgba(255, 231, 74, 0.42);
  color: var(--color-primary);
}

.sidebar-toggle__label {
  font-size: 11px;
  letter-spacing: 0.12em;
  text-transform: uppercase;
}
</style>
