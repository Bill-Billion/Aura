<script setup lang="ts">
import { computed, ref } from 'vue'
import { useUIStore } from '@/stores/uiStore'
import { useWorldStore } from '@/stores/worldStore'
import { useWebSocket } from '@/composables/useWebSocket'
import { getFloorForDevice } from '@/utils/deviceFloorMap'

const uiStore = useUIStore()
const worldStore = useWorldStore()
const { sendCommand } = useWebSocket()

const floorMeta: Record<string, { title: string; summary: string }> = {
  overview: { title: '总览', summary: '总览态只保留一层为锚点，二层和三层退到背景。' },
  F1: { title: 'F1 客厅层', summary: '客厅、餐厨和主要交互区。' },
  F2: { title: 'F2 私密层', summary: '卧室和卫浴压低亮度，保持退后。' },
  F3: { title: 'F3 服务层', summary: '服务空间只保留轮廓和层次。' },
}

const deviceNameMap: Record<string, string> = {
  light_living_01: '客厅灯光',
  light_bedroom_01: '卧室灯光',
  ac_living_01: '客厅空调',
  curtain_living_01: '客厅窗帘',
}

const sceneModes = [
  { id: 'reading', label: '阅读模式', desc: '局部暖光和半开窗帘' },
  { id: 'entertainment', label: '娱乐模式', desc: '保持低亮度，强化客厅氛围' },
  { id: 'away', label: '离家模式', desc: '收束灯光和窗帘，压低存在感' },
  { id: 'sleep', label: '睡眠模式', desc: '关闭主要灯光，空调归夜间值' },
] as const

const activeSceneId = ref<(typeof sceneModes)[number]['id']>('entertainment')

const liveMetrics = computed(() => {
  const devices = Object.values(worldStore.devices)
  const lightsOn = devices.filter((device) => device.type === 'light' && device.state.power).length
  const hvacOn = devices.filter((device) => device.type === 'hvac' && device.state.power).length
  const curtainsOpen = devices.filter((device) => device.type === 'curtain' && (device.state.extra.open_percent ?? 0) > 0).length

  return [
    { label: '在线设备', value: `${devices.length}` },
    { label: '灯光', value: `${lightsOn}` },
    { label: '空调', value: `${hvacOn}` },
    { label: '窗帘', value: `${curtainsOpen}` },
  ]
})

const floorSections = computed(() => {
  const buckets: Record<string, Array<{ id: string; label: string; type: string; online: boolean }>> = {
    F1: [],
    F2: [],
    F3: [],
  }

  for (const [deviceId, device] of Object.entries(worldStore.devices)) {
    const floorId = getFloorForDevice(deviceId, device.location.room)
    if (!floorId || !buckets[floorId]) continue
    buckets[floorId].push({
      id: deviceId,
      label: deviceNameMap[deviceId] ?? deviceId,
      type: device.type,
      online: Boolean(device.state.power),
    })
  }

  return Object.entries(buckets).map(([floorId, devices]) => ({
    floorId,
    title: floorMeta[floorId].title,
    summary: floorMeta[floorId].summary,
    devices,
  }))
})

const panelHeader = computed(() => floorMeta[uiStore.activeFloor] ?? floorMeta.overview)
const currentScene = computed(() => sceneModes.find((mode) => mode.id === activeSceneId.value) ?? sceneModes[1])
const liveDeckLabel = computed(() => (uiStore.activeFloor === 'overview' ? 'F1' : uiStore.activeFloor))

const primarySection = computed(() => {
  // 总览态只保留一层摘要，避免右侧重新长成控制台。
  const floorId = uiStore.activeFloor === 'overview' ? 'F1' : uiStore.activeFloor
  return floorSections.value.find((section) => section.floorId === floorId) ?? floorSections.value[0]
})

function applyScene(sceneId: (typeof sceneModes)[number]['id']) {
  activeSceneId.value = sceneId
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
}

function focusDevice(deviceId: string, floorId: string) {
  uiStore.setActiveFloor(floorId)
  uiStore.setActiveDevice(deviceId)
}
</script>

<template>
  <section class="home-panel-group">
    <article class="showroom-card showroom-card--live">
      <div class="live-preview">
        <div class="live-preview__header">
          <span class="live-preview__tag">LIVE</span>
          <span class="live-preview__floor">{{ liveDeckLabel }}</span>
        </div>
        <div class="live-preview__body">
          <p class="live-preview__title">{{ panelHeader.title }}</p>
          <p class="live-preview__text">{{ panelHeader.summary }}</p>
        </div>
      </div>

      <div class="live-metrics">
        <div v-for="metric in liveMetrics" :key="metric.label" class="live-metric">
          <span class="live-metric__label">{{ metric.label }}</span>
          <span class="live-metric__value">{{ metric.value }}</span>
        </div>
      </div>
    </article>

    <article class="showroom-card showroom-card--modes">
      <header class="showroom-card__header">
        <span>场景模式</span>
        <button class="showroom-card__ghost" @click="uiStore.sceneSelectorOpen = true">更多</button>
      </header>

      <button
        v-for="mode in sceneModes"
        :key="mode.id"
        class="scene-row"
        :class="{ active: activeSceneId === mode.id }"
        @click="applyScene(mode.id)"
      >
        <div class="scene-row__body">
          <span class="scene-row__title">{{ mode.label }}</span>
          <span class="scene-row__desc">{{ mode.desc }}</span>
        </div>
      </button>
    </article>

    <article v-if="primarySection" class="showroom-card showroom-card--summary">
      <header class="showroom-card__header">
        <span>{{ primarySection.title }}</span>
        <span class="showroom-card__meta">{{ primarySection.devices.length }} 个设备入口</span>
      </header>
      <p class="showroom-card__text">{{ uiStore.activeFloor === 'overview' ? currentScene.desc : primarySection.summary }}</p>

      <div v-if="primarySection.devices.length > 0" class="device-chip-list">
        <button
          v-for="device in primarySection.devices"
          :key="device.id"
          class="device-chip"
          :class="{
            active: uiStore.activeDevice === device.id,
            online: device.online,
          }"
          @click="focusDevice(device.id, primarySection.floorId)"
        >
          <span class="device-chip__type">{{ device.type }}</span>
          <span class="device-chip__label">{{ device.label }}</span>
        </button>
      </div>
      <p v-else class="showroom-card__empty">这一层暂时没有接入可控设备。</p>
    </article>
  </section>
</template>

<style scoped>
.home-panel-group {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.showroom-card {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.showroom-card__header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  font-size: 12px;
  color: var(--color-text-primary);
}

.showroom-card__text,
.showroom-card__empty,
.showroom-card__meta {
  margin: 0;
  color: var(--color-text-secondary);
  font-size: 12px;
  line-height: 1.5;
}

.showroom-card__ghost {
  border: 1px solid var(--color-border);
  border-radius: 999px;
  background: rgba(255, 255, 255, 0.02);
  color: var(--color-text-secondary);
  padding: 5px 10px;
  cursor: pointer;
  transition: all var(--transition-fast);
}

.showroom-card__ghost:hover {
  color: var(--color-text-primary);
  border-color: rgba(255, 231, 74, 0.3);
}

.live-preview {
  position: relative;
  overflow: hidden;
  min-height: 152px;
  padding: 12px;
  border: 1px solid rgba(255, 255, 255, 0.08);
  background:
    linear-gradient(180deg, rgba(255, 255, 255, 0.06), rgba(255, 255, 255, 0.01)),
    linear-gradient(120deg, rgba(34, 40, 50, 0.96) 0%, rgba(24, 30, 37, 0.92) 52%, rgba(15, 18, 22, 0.98) 100%);
}

.live-preview::after {
  content: '';
  position: absolute;
  inset: 0;
  background:
    linear-gradient(90deg, transparent 0%, rgba(255, 255, 255, 0.06) 46%, rgba(255, 255, 255, 0.02) 52%, transparent 100%),
    repeating-linear-gradient(90deg, rgba(255, 255, 255, 0.03) 0, rgba(255, 255, 255, 0.03) 1px, transparent 1px, transparent 48px);
  opacity: 0.8;
  pointer-events: none;
}

.live-preview__header,
.live-preview__body {
  position: relative;
  z-index: 1;
}

.live-preview__header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 56px;
}

.live-preview__tag,
.live-preview__floor {
  font-size: 11px;
  letter-spacing: 0.12em;
  text-transform: uppercase;
}

.live-preview__tag {
  color: var(--color-primary);
}

.live-preview__floor {
  color: rgba(244, 246, 248, 0.88);
}

.live-preview__title {
  margin: 0 0 6px;
  font-size: 26px;
  letter-spacing: -0.04em;
  color: var(--color-text-primary);
}

.live-preview__text {
  margin: 0;
  max-width: 180px;
  font-size: 12px;
  line-height: 1.5;
  color: var(--color-text-secondary);
}

.live-metrics {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 8px;
}

.live-metric {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 10px;
  padding: 8px 0;
  border-top: 1px solid rgba(255, 255, 255, 0.06);
}

.live-metric__label {
  font-size: 11px;
  color: var(--color-text-muted);
}

.live-metric__value {
  font-size: 18px;
  color: var(--color-text-primary);
}

.scene-row {
  display: flex;
  align-items: center;
  width: 100%;
  min-height: 74px;
  padding: 0 12px;
  border: 1px solid transparent;
  background: rgba(255, 255, 255, 0.03);
  color: var(--color-text-secondary);
  cursor: pointer;
  transition: all var(--transition-fast);
  text-align: left;
}

.scene-row:hover {
  border-color: rgba(255, 255, 255, 0.08);
  color: var(--color-text-primary);
}

.scene-row.active {
  background: rgba(255, 231, 74, 0.92);
  border-color: rgba(255, 231, 74, 0.92);
  color: #17191d;
}

.scene-row__body {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.scene-row__title {
  font-size: 17px;
}

.scene-row__desc {
  font-size: 12px;
  line-height: 1.4;
  opacity: 0.78;
}

.device-chip-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.device-chip {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 12px;
  border: 1px solid rgba(255, 255, 255, 0.08);
  background: rgba(255, 255, 255, 0.02);
  color: var(--color-text-secondary);
  text-align: left;
  cursor: pointer;
  transition: all var(--transition-fast);
}

.device-chip:hover {
  border-color: rgba(255, 255, 255, 0.14);
  color: var(--color-text-primary);
}

.device-chip.active {
  border-color: rgba(255, 231, 74, 0.48);
  color: var(--color-primary);
}

.device-chip.online .device-chip__type {
  color: var(--color-primary);
}

.device-chip__type {
  min-width: 42px;
  font-size: 10px;
  letter-spacing: 0.16em;
  text-transform: uppercase;
  color: var(--color-text-muted);
}

.device-chip__label {
  font-size: 13px;
}
</style>
