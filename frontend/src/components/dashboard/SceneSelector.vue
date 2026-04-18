<script setup lang="ts">
import { useUIStore } from '@/stores/uiStore'
import { useWebSocket } from '@/composables/useWebSocket'
import { useWorldStore } from '@/stores/worldStore'

const uiStore = useUIStore()
const worldStore = useWorldStore()
const { sendCommand } = useWebSocket()

const scenes = [
  { id: 'reading', label: '阅读模式', desc: '局部暖光和半开窗帘' },
  { id: 'entertainment', label: '娱乐模式', desc: '保持低亮度，强化客厅氛围' },
  { id: 'away', label: '离家模式', desc: '收束灯光和窗帘，压低存在感' },
  { id: 'sleep', label: '睡眠模式', desc: '关闭主要灯光，空调归夜间值' },
]

function applyScene(sceneId: string) {
  const devices = Object.entries(worldStore.devices)

  switch (sceneId) {
    case 'reading':
      for (const [id, dev] of devices) {
        if (dev.type === 'light') {
          sendCommand('CMD_DEVICE_CONTROL', { device_id: id, action: 'turn_on' })
          sendCommand('CMD_DEVICE_CONTROL', { device_id: id, action: 'set_state', params: { brightness: 68, color_temp: 3400 } })
        }
        if (dev.type === 'curtain') {
          sendCommand('CMD_DEVICE_CONTROL', { device_id: id, action: 'set_state', params: { open_percent: 52 } })
        }
      }
      break
    case 'entertainment':
      for (const [id, dev] of devices) {
        if (dev.type === 'light') {
          sendCommand('CMD_DEVICE_CONTROL', { device_id: id, action: 'turn_on' })
          sendCommand('CMD_DEVICE_CONTROL', { device_id: id, action: 'set_state', params: { brightness: 42, color_temp: 2850 } })
        }
        if (dev.type === 'curtain') {
          sendCommand('CMD_DEVICE_CONTROL', { device_id: id, action: 'set_state', params: { open_percent: 28 } })
        }
        if (dev.type === 'hvac') {
          sendCommand('CMD_DEVICE_CONTROL', { device_id: id, action: 'turn_on' })
          sendCommand('CMD_DEVICE_CONTROL', { device_id: id, action: 'set_state', params: { target_temp: 24, mode: 'cool' } })
        }
      }
      break
    case 'away':
      for (const [id, dev] of devices) {
        if (dev.type === 'light' || dev.type === 'hvac') {
          sendCommand('CMD_DEVICE_CONTROL', { device_id: id, action: 'turn_off' })
        }
        if (dev.type === 'curtain') {
          sendCommand('CMD_DEVICE_CONTROL', { device_id: id, action: 'set_state', params: { open_percent: 0 } })
        }
      }
      break
    case 'sleep':
      for (const [id, dev] of devices) {
        if (dev.type === 'light') {
          sendCommand('CMD_DEVICE_CONTROL', { device_id: id, action: 'turn_off' })
        }
        if (dev.type === 'curtain') {
          sendCommand('CMD_DEVICE_CONTROL', { device_id: id, action: 'set_state', params: { open_percent: 0 } })
        }
        if (dev.type === 'hvac') {
          sendCommand('CMD_DEVICE_CONTROL', { device_id: id, action: 'turn_on' })
          sendCommand('CMD_DEVICE_CONTROL', { device_id: id, action: 'set_state', params: { target_temp: 22, mode: 'cool' } })
        }
      }
      break
  }

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
