<script setup lang="ts">
/**
 * 场景面板：点一下发**一条** CMD_SCENE_APPLY，不再在浏览器里循环发 2×N 条直控。
 *
 * 场景要展开成哪些设备命令，由后端 SceneAgent 按 scene_definitions.yaml 决定，并与其他
 * agent 走同一条编排 → 仲裁 → CommandExecutor 的腿。因此这个组件里**不该再出现任何设备
 * 取值**（亮度/色温/开合度…）——那些值回到这里，就等于场景语义又漏回了浏览器。
 */
import { useUIStore } from '@/stores/uiStore'
import { useWebSocket } from '@/composables/useWebSocket'
import {
  SCENE_APPLY_COMMAND,
  SCENE_PRESETS,
  buildSceneApplyPayload,
} from '@/utils/scenePresets'

const uiStore = useUIStore()
const { sendCommand } = useWebSocket()

const scenes = SCENE_PRESETS

function applyScene(sceneId: string) {
  sendCommand(SCENE_APPLY_COMMAND, buildSceneApplyPayload(sceneId))
  uiStore.sceneSelectorOpen = false
}
</script>

<template>
  <div class="scene-backdrop" @click.self="uiStore.sceneSelectorOpen = false">
    <div class="scene-selector showroom-card">
      <div class="selector-header">
        <div>
          <p class="selector-eyebrow">Scene Presets</p>
          <h2 class="selector-title">场景预设</h2>
        </div>
        <button class="selector-close" @click="uiStore.sceneSelectorOpen = false">关闭</button>
      </div>
      <div class="scene-list">
        <button
          v-for="scene in scenes"
          :key="scene.id"
          class="scene-item"
          @click="applyScene(scene.id)"
        >
          <div class="scene-info">
            <span class="scene-label">{{ scene.label }}</span>
            <span class="scene-desc">{{ scene.desc }}</span>
          </div>
        </button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.scene-backdrop {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  background: rgba(5, 7, 10, 0.48);
  backdrop-filter: blur(12px);
  z-index: var(--z-modal);
  pointer-events: auto;
}

.scene-selector {
  width: min(360px, calc(100vw - 32px));
}

.selector-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 12px;
}

.selector-eyebrow {
  margin: 0 0 6px;
  font-size: 10px;
  letter-spacing: 0.18em;
  text-transform: uppercase;
  color: var(--color-text-muted);
}

.selector-title {
  margin: 0;
  font-size: 22px;
  font-weight: 600;
}

.selector-close {
  min-width: 58px;
  height: 30px;
  border: 1px solid var(--color-border);
  border-radius: 999px;
  background: rgba(255, 255, 255, 0.03);
  color: var(--color-text-secondary);
  cursor: pointer;
}

.scene-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.scene-item {
  display: flex;
  align-items: center;
  gap: 12px;
  min-height: 72px;
  padding: 0 12px;
  border: 1px solid rgba(255, 255, 255, 0.08);
  background: rgba(255, 255, 255, 0.03);
  color: var(--color-text-primary);
  cursor: pointer;
  text-align: left;
  transition: all var(--transition-fast);
}

.scene-item:hover {
  border-color: rgba(255, 231, 74, 0.4);
}

.scene-info {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.scene-label {
  font-size: 16px;
}

.scene-desc {
  font-size: 12px;
  color: var(--color-text-secondary);
  line-height: 1.5;
}
</style>
