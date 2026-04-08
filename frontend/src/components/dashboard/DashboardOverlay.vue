<script setup lang="ts">
import { computed } from 'vue'
import StatusBar from './StatusBar.vue'
import FloorSelector from './FloorSelector.vue'
import HomePanelGroup from './HomePanelGroup.vue'
import SimControlBar from './SimControlBar.vue'
import AIChatPanel from './AIChatPanel.vue'
import SidebarToggle from './SidebarToggle.vue'
import SceneSelector from './SceneSelector.vue'
import { useUIStore } from '@/stores/uiStore'

const uiStore = useUIStore()

// Hide right panel when focused on a specific floor (not overview)
const showRightPanel = computed(() => uiStore.activeFloor === 'overview')
</script>

<template>
  <div class="overlay-root no-select">
    <!-- Left: Floor selector -->
    <FloorSelector class="zone-left" />

    <!-- Right top: Device control panels (auto-hide when floor focused) -->
    <Transition name="slide-panel-right">
      <HomePanelGroup v-if="showRightPanel" class="zone-right-top" />
    </Transition>

    <!-- Bottom center: Status / weather -->
    <StatusBar class="zone-bottom" />

    <!-- Bottom left: Simulation controls -->
    <SimControlBar class="zone-bottom-left" />

    <!-- Right: Sidebar toggle -->
    <SidebarToggle class="zone-sidebar-toggle" />

    <!-- Right sidebar: AI chat panel -->
    <Transition name="slide-right">
      <AIChatPanel v-if="uiStore.sidebarOpen" class="zone-sidebar" />
    </Transition>

    <!-- Scene selector modal -->
    <SceneSelector v-if="uiStore.sceneSelectorOpen" />
  </div>
</template>

<style scoped>
.overlay-root {
  position: absolute;
  inset: 0;
  pointer-events: none;
  z-index: var(--z-overlay);
}

.zone-left {
  position: absolute;
  left: var(--spacing-panel);
  top: 50%;
  transform: translateY(-50%);
  pointer-events: auto;
}

.zone-right-top {
  position: absolute;
  top: var(--spacing-panel);
  right: var(--spacing-panel-right);
  pointer-events: auto;
}

.zone-bottom {
  position: absolute;
  bottom: var(--spacing-panel);
  left: 50%;
  transform: translateX(-50%);
  pointer-events: auto;
}

.zone-bottom-left {
  position: absolute;
  bottom: var(--spacing-panel);
  left: var(--spacing-panel);
  pointer-events: auto;
}

.zone-sidebar-toggle {
  position: absolute;
  top: var(--spacing-panel);
  right: var(--spacing-panel);
  pointer-events: auto;
}

.zone-sidebar {
  position: absolute;
  top: 0;
  right: 0;
  bottom: 0;
  width: 360px;
  pointer-events: auto;
}

/* Right panel slide transition */
.slide-panel-right-enter-active,
.slide-panel-right-leave-active {
  transition: transform 0.4s ease, opacity 0.4s ease;
}

.slide-panel-right-enter-from,
.slide-panel-right-leave-to {
  transform: translateX(30px);
  opacity: 0;
}

/* Sidebar slide transition */
.slide-right-enter-active,
.slide-right-leave-active {
  transition: transform var(--transition-slow);
}

.slide-right-enter-from,
.slide-right-leave-to {
  transform: translateX(100%);
}
</style>
