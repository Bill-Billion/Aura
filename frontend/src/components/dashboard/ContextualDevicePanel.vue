<script setup lang="ts">
import { computed } from 'vue'
import { useUIStore } from '@/stores/uiStore'
import { useWorldStore } from '@/stores/worldStore'
import LightControlPanel from './panels/LightControlPanel.vue'
import HVACControlPanel from './panels/HVACControlPanel.vue'
import CurtainControlPanel from './panels/CurtainControlPanel.vue'

const uiStore = useUIStore()
const worldStore = useWorldStore()

const deviceId = computed(() => uiStore.activeDevice)
const device = computed(() => (deviceId.value ? worldStore.devices[deviceId.value] : null))

const deviceTypeLabel = computed(() => {
  if (!device.value) return ''
  const labels: Record<string, string> = {
    light: '灯光控制',
    hvac: '空调控制',
    curtain: '窗帘控制',
  }
  return labels[device.value.type] ?? '设备控制'
})

const roomLabel = computed(() => {
  const labels: Record<string, string> = {
    living_room: '一层客厅',
    kitchen: '一层厨房',
    bedroom: '二层卧室',
    bathroom: '二层卫浴',
  }
  return labels[device.value?.location.room ?? ''] ?? '场景设备'
})

function closePanel() {
  uiStore.setActiveDevice(null)
}
</script>

<template>
  <Transition name="contextual-panel">
    <section v-if="device && deviceId" class="contextual-device-panel showroom-card">
      <header class="contextual-device-panel__header">
        <div>
          <p class="contextual-device-panel__eyebrow">选中对象</p>
          <h3 class="contextual-device-panel__title">{{ deviceTypeLabel }}</h3>
          <p class="contextual-device-panel__sub">{{ roomLabel }} · {{ deviceId }}</p>
        </div>
        <button class="contextual-device-panel__close" @click="closePanel">关闭</button>
      </header>

      <LightControlPanel
        v-if="device.type === 'light'"
        :device-id="deviceId"
        :device="device"
      />
      <HVACControlPanel
        v-else-if="device.type === 'hvac'"
        :device-id="deviceId"
        :device="device"
      />
      <CurtainControlPanel
        v-else-if="device.type === 'curtain'"
        :device-id="deviceId"
        :device="device"
      />
    </section>
  </Transition>
</template>

<style scoped>
.contextual-device-panel {
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding: 14px;
}

.contextual-device-panel__header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
}

.contextual-device-panel__eyebrow {
  margin: 0 0 4px;
  font-size: 11px;
  letter-spacing: 0.14em;
  text-transform: uppercase;
  color: var(--color-text-muted);
}

.contextual-device-panel__title {
  margin: 0;
  font-size: 18px;
  font-weight: 600;
  color: var(--color-text-primary);
}

.contextual-device-panel__sub {
  margin: 6px 0 0;
  font-size: 12px;
  color: var(--color-text-secondary);
}

.contextual-device-panel__close {
  min-width: 58px;
  height: 30px;
  border: 1px solid var(--color-border);
  border-radius: 999px;
  background: rgba(255, 255, 255, 0.02);
  color: var(--color-text-secondary);
  cursor: pointer;
  transition: all var(--transition-fast);
}

.contextual-device-panel__close:hover {
  color: var(--color-text-primary);
  border-color: rgba(255, 231, 74, 0.36);
}

.contextual-panel-enter-active,
.contextual-panel-leave-active {
  transition: transform var(--transition-normal), opacity var(--transition-normal);
}

.contextual-panel-enter-from,
.contextual-panel-leave-to {
  transform: translateY(12px);
  opacity: 0;
}
</style>
