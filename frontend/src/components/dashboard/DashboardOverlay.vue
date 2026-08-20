<script setup lang="ts">
import { nextTick, ref } from 'vue'
import StatusBar from './StatusBar.vue'
import FloorSelector from './FloorSelector.vue'
import HomePanelGroup from './HomePanelGroup.vue'
import SimControlBar from './SimControlBar.vue'
import ObservabilityPanel from './ObservabilityPanel.vue'
import SidebarToggle from './SidebarToggle.vue'
import SceneSelector from './SceneSelector.vue'
import ContextualDevicePanel from './ContextualDevicePanel.vue'
import ResearchWorkspace from './ResearchWorkspace.vue'
import { useUIStore } from '@/stores/uiStore'
import { showroomVisualConfig } from '@/config/showroomVisualConfig'

const uiStore = useUIStore()
const researchLauncher = ref<HTMLButtonElement | null>(null)

function openResearchWorkspace() {
  uiStore.openResearchWorkspace()
}

async function closeResearchWorkspace() {
  uiStore.closeResearchWorkspace()
  await nextTick()
  researchLauncher.value?.focus()
}
</script>

<template>
  <div
    class="overlay-root no-select"
    :style="{ '--showroom-panel-width': `${showroomVisualConfig.overlay.panelWidth}px` }"
  >
    <FloorSelector class="zone-left" :inert="uiStore.researchWorkspaceOpen" />

    <aside class="zone-right" :inert="uiStore.researchWorkspaceOpen">
      <StatusBar class="zone-right__status" />
      <ContextualDevicePanel />
      <HomePanelGroup class="zone-right__content" />
    </aside>

    <div class="zone-bottom-left" :inert="uiStore.researchWorkspaceOpen">
      <SimControlBar />
      <SidebarToggle />
      <button
        ref="researchLauncher"
        class="research-launcher"
        type="button"
        :aria-expanded="uiStore.researchWorkspaceOpen"
        aria-controls="research-workspace"
        @click="openResearchWorkspace"
      >
        <span class="research-launcher__index">R·01</span>
        <span>研究运行</span>
      </button>
    </div>

    <Transition name="slide-right">
      <ObservabilityPanel
        v-if="uiStore.sidebarOpen"
        class="zone-sidebar"
        :inert="uiStore.researchWorkspaceOpen"
      />
    </Transition>

    <SceneSelector v-if="uiStore.sceneSelectorOpen" :inert="uiStore.researchWorkspaceOpen" />

    <ResearchWorkspace
      id="research-workspace"
      v-show="uiStore.researchWorkspaceOpen"
      :open="uiStore.researchWorkspaceOpen"
      @close="closeResearchWorkspace"
    />
  </div>
</template>

<style scoped>
.overlay-root {
  position: absolute;
  inset: 0;
  pointer-events: none;
  z-index: var(--z-overlay);
  --showroom-sidebar-width: 400px;
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
  width: var(--showroom-sidebar-width);
  pointer-events: auto;
}

.research-launcher {
  min-height: 40px;
  padding: 0 13px;
  display: inline-flex;
  align-items: center;
  gap: 8px;
  border: 1px solid rgba(255, 231, 74, 0.28);
  border-radius: 999px;
  background: rgba(255, 231, 74, 0.08);
  color: var(--color-primary);
  cursor: pointer;
  transition-property: border-color, background-color, color, transform;
  transition-duration: var(--transition-fast);
}

.research-launcher__index {
  font-family: ui-monospace, "SFMono-Regular", Menlo, monospace;
  font-size: 9px;
  letter-spacing: 0.08em;
  color: var(--color-text-muted);
}

.research-launcher:active {
  transform: scale(0.96);
}

.research-launcher:focus-visible {
  outline: 2px solid var(--color-primary);
  outline-offset: 2px;
}

@media (hover: hover) {
  .research-launcher:hover {
    border-color: rgba(255, 231, 74, 0.5);
    background: rgba(255, 231, 74, 0.12);
  }
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

@media (min-width: 1920px) {
  .overlay-root {
    --showroom-sidebar-width: 480px;
  }
}

@media (max-width: 1365px) {
  .zone-sidebar {
    left: 0;
    width: 100vw;
  }
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

@media (prefers-reduced-motion: reduce) {
  .research-launcher {
    transition-duration: 0.01ms;
  }
}
</style>
