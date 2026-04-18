<script setup lang="ts">
import StatusBar from './StatusBar.vue'
import FloorSelector from './FloorSelector.vue'
import HomePanelGroup from './HomePanelGroup.vue'
import SimControlBar from './SimControlBar.vue'
import AIChatPanel from './AIChatPanel.vue'
import SidebarToggle from './SidebarToggle.vue'
import SceneSelector from './SceneSelector.vue'
import ContextualDevicePanel from './ContextualDevicePanel.vue'
import { useUIStore } from '@/stores/uiStore'
import { showroomVisualConfig } from '@/config/showroomVisualConfig'

const uiStore = useUIStore()
</script>

<template>
  <div class="overlay-root no-select" :style="{ '--showroom-panel-width': `${showroomVisualConfig.overlay.panelWidth}px` }">
    <FloorSelector class="zone-left" />

    <aside class="zone-right">
      <StatusBar class="zone-right__status" />
      <ContextualDevicePanel />
      <HomePanelGroup class="zone-right__content" />
    </aside>

    <div class="zone-bottom-left">
      <SimControlBar />
      <SidebarToggle />
    </div>

    <Transition name="slide-right">
      <AIChatPanel v-if="uiStore.sidebarOpen" class="zone-sidebar" />
    </Transition>

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

.zone-right {
  position: absolute;
  top: var(--spacing-panel);
  right: var(--spacing-panel-right);
  bottom: calc(var(--spacing-panel) + 22px);
  width: min(var(--showroom-panel-width), calc(100vw - 220px));
  display: flex;
  flex-direction: column;
  gap: 10px;
  pointer-events: auto;
}

.zone-right__content {
  overflow-y: auto;
  padding-right: 2px;
}

.zone-bottom-left {
  position: absolute;
  left: var(--spacing-panel);
  bottom: var(--spacing-panel);
  display: flex;
  align-items: center;
  gap: 10px;
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

.slide-right-enter-active,
.slide-right-leave-active {
  transition: transform var(--transition-slow), opacity var(--transition-slow);
}

.slide-right-enter-from,
.slide-right-leave-to {
  transform: translateX(100%);
  opacity: 0;
}

@media (max-width: 1200px) {
  .zone-right {
    width: min(var(--showroom-panel-width), calc(100vw - 180px));
  }
}

@media (max-width: 920px) {
  .zone-right {
    left: 116px;
    width: auto;
  }
}

@media (max-width: 820px) {
  .zone-right {
    top: auto;
    right: var(--spacing-panel);
    left: var(--spacing-panel);
    bottom: 84px;
  }

  .zone-left {
    top: var(--spacing-panel);
    transform: none;
  }

  .zone-bottom-left {
    right: var(--spacing-panel);
    justify-content: space-between;
  }
}
</style>
