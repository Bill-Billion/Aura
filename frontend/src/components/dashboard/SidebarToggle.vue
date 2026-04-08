<script setup lang="ts">
import { useUIStore } from '@/stores/uiStore'

const uiStore = useUIStore()
</script>

<template>
  <button
    class="sidebar-toggle"
    :class="{ active: uiStore.sidebarOpen }"
    @click="uiStore.toggleSidebar()"
    title="AI 助手"
  >
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
      <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" />
    </svg>
  </button>
</template>

<style scoped>
.sidebar-toggle {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 36px;
  height: 36px;
  border: var(--panel-border);
  border-radius: var(--panel-radius);
  background: var(--panel-bg);
  backdrop-filter: var(--panel-blur);
  -webkit-backdrop-filter: var(--panel-blur);
  color: var(--color-text-secondary);
  cursor: pointer;
  transition: all var(--transition-normal);
}

.sidebar-toggle:hover {
  color: var(--color-text-primary);
  border-color: var(--color-border-strong);
}

.sidebar-toggle.active {
  color: var(--color-primary);
  border-color: rgba(255, 231, 74, 0.3);
  background: rgba(255, 231, 74, 0.08);
}
</style>
