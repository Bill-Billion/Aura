<script setup lang="ts">
import { computed } from 'vue'
import { useWorldStore } from '@/stores/worldStore'
import { useUIStore } from '@/stores/uiStore'
import LightControlPanel from './panels/LightControlPanel.vue'
import HVACControlPanel from './panels/HVACControlPanel.vue'
import CurtainControlPanel from './panels/CurtainControlPanel.vue'

const worldStore = useWorldStore()
const uiStore = useUIStore()

const ROOM_LABELS: Record<string, string> = {
  living_room: '客厅',
  bedroom: '卧室',
  kitchen: '厨房',
  bathroom: '浴室',
}

const activeRoomLabel = computed(() => ROOM_LABELS[uiStore.activeRoom] ?? uiStore.activeRoom)

const roomDevices = computed(() => {
  return Object.entries(worldStore.devices).filter(
    ([, dev]) => dev.location.room === uiStore.activeRoom
  )
})
</script>

<template>
  <div class="home-panel-group">
    <div class="panel-header">
      <span class="room-name">{{ activeRoomLabel }}</span>
      <span class="device-count">{{ roomDevices.length }} 设备</span>
    </div>

    <div class="panel-list">
      <template v-for="[deviceId, device] in roomDevices" :key="deviceId">
        <LightControlPanel
          v-if="device.type === 'light'"
          :device-id="deviceId"
          :device="device"
        />
        <HVACControlPanel
          v-if="device.type === 'hvac'"
          :device-id="deviceId"
          :device="device"
        />
        <CurtainControlPanel
          v-if="device.type === 'curtain'"
          :device-id="deviceId"
          :device="device"
        />
      </template>

      <div v-if="roomDevices.length === 0" class="no-devices">
        当前房间无设备
      </div>
    </div>
  </div>
</template>

<style scoped>
.home-panel-group {
  width: 280px;
  max-height: calc(100vh - 120px);
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.panel-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 0 4px;
}

.room-name {
  font-size: 16px;
  font-weight: 700;
  color: var(--color-text-primary);
}

.device-count {
  font-size: 11px;
  color: var(--color-text-muted);
}

.panel-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
  overflow-y: auto;
  padding-right: 4px;
}

.no-devices {
  text-align: center;
  color: var(--color-text-muted);
  font-size: 12px;
  padding: 24px 0;
}
</style>
