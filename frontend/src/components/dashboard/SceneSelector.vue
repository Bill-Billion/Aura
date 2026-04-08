<script setup lang="ts">
import { useUIStore } from '@/stores/uiStore'
import { useWebSocket } from '@/composables/useWebSocket'
import { useWorldStore } from '@/stores/worldStore'

const uiStore = useUIStore()
const worldStore = useWorldStore()
const { sendCommand } = useWebSocket()

const scenes = [
  { id: 'morning', label: '清晨模式', icon: '🌅', desc: '开灯 75%、暖光、开窗帘' },
  { id: 'day', label: '白天模式', icon: '☀️', desc: '关灯、开窗帘' },
  { id: 'evening', label: '傍晚模式', icon: '🌇', desc: '开灯 60%、暖光' },
  { id: 'night', label: '夜间模式', icon: '🌙', desc: '关灯、关窗帘、降温' },
]

function applyScene(sceneId: string) {
  const devices = Object.entries(worldStore.devices)

  switch (sceneId) {
    case 'morning':
      for (const [id, dev] of devices) {
        if (dev.type === 'light') {
          sendCommand('CMD_DEVICE_CONTROL', { device_id: id, action: 'turn_on' })
          sendCommand('CMD_DEVICE_CONTROL', { device_id: id, action: 'set_state', params: { brightness: 75, color_temp: 3000 } })
        }
        if (dev.type === 'curtain') {
          sendCommand('CMD_DEVICE_CONTROL', { device_id: id, action: 'set_state', params: { open_percent: 80 } })
        }
      }
      break
    case 'day':
      for (const [id, dev] of devices) {
        if (dev.type === 'light') sendCommand('CMD_DEVICE_CONTROL', { device_id: id, action: 'turn_off' })
        if (dev.type === 'curtain') sendCommand('CMD_DEVICE_CONTROL', { device_id: id, action: 'set_state', params: { open_percent: 100 } })
      }
      break
    case 'evening':
      for (const [id, dev] of devices) {
        if (dev.type === 'light') {
          sendCommand('CMD_DEVICE_CONTROL', { device_id: id, action: 'turn_on' })
          sendCommand('CMD_DEVICE_CONTROL', { device_id: id, action: 'set_state', params: { brightness: 60, color_temp: 2700 } })
        }
      }
      break
    case 'night':
      for (const [id, dev] of devices) {
        if (dev.type === 'light') sendCommand('CMD_DEVICE_CONTROL', { device_id: id, action: 'turn_off' })
        if (dev.type === 'curtain') sendCommand('CMD_DEVICE_CONTROL', { device_id: id, action: 'set_state', params: { open_percent: 0 } })
        if (dev.type === 'hvac') sendCommand('CMD_DEVICE_CONTROL', { device_id: id, action: 'set_state', params: { target_temp: 22 } })
      }
      break
  }

  uiStore.sceneSelectorOpen = false
}
</script>

<template>
  <div class="scene-backdrop" @click.self="uiStore.sceneSelectorOpen = false">
    <div class="scene-selector glass-panel">
      <div class="selector-header">
        <span class="selector-title">场景预设</span>
      </div>
      <div class="scene-list">
        <button
          v-for="scene in scenes"
          :key="scene.id"
          class="scene-item"
          @click="applyScene(scene.id)"
        >
          <span class="scene-icon">{{ scene.icon }}</span>
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
  z-index: var(--z-modal);
  pointer-events: auto;
}

.scene-selector {
  width: 300px;
  padding: 16px;
}

.selector-header {
  margin-bottom: 12px;
}

.selector-title {
  font-size: 14px;
  font-weight: 700;
  color: var(--color-text-primary);
}

.scene-list {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.scene-item {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 12px;
  border: none;
  border-radius: 8px;
  background: transparent;
  cursor: pointer;
  transition: all var(--transition-fast);
  text-align: left;
}

.scene-item:hover {
  background: rgba(255, 255, 255, 0.06);
}

.scene-item:active {
  background: var(--color-primary);
}

.scene-item:active .scene-label,
.scene-item:active .scene-desc {
  color: #000;
}

.scene-icon {
  font-size: 24px;
  flex-shrink: 0;
}

.scene-info {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.scene-label {
  font-size: 13px;
  font-weight: 600;
  color: var(--color-text-primary);
}

.scene-desc {
  font-size: 10px;
  color: var(--color-text-secondary);
}
</style>
