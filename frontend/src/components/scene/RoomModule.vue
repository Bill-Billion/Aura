<script setup lang="ts">
import type { RoomConfig } from '@/types/scene-config'
import DeviceVisual from './DeviceVisual.vue'
import RoomShell from './rooms/RoomShell.vue'
import LivingRoomFurniture from './rooms/LivingRoomFurniture.vue'
import BedroomFurniture from './rooms/BedroomFurniture.vue'
import KitchenFurniture from './rooms/KitchenFurniture.vue'
import BathroomFurniture from './rooms/BathroomFurniture.vue'

const props = defineProps<{
  room: RoomConfig
}>()

const deviceEntries = Object.entries(props.room.devices)

// Modern smart-home palette (Mi Home style)
const roomConfigs: Record<string, { width: number; depth: number; height: number; wallColor: string; floorColor: string }> = {
  living_room: { width: 5.5, depth: 5.5, height: 3, wallColor: '#f5f5f0', floorColor: '#c4a67a' },
  bedroom:     { width: 5, depth: 5, height: 3, wallColor: '#f2f0ec', floorColor: '#b89b72' },
  kitchen:     { width: 4.5, depth: 4.5, height: 3, wallColor: '#f5f5f0', floorColor: '#d6cfc4' },
  bathroom:    { width: 3.8, depth: 4.5, height: 3, wallColor: '#eef0f0', floorColor: '#c8c4be' },
}

const cfg = roomConfigs[props.room.type] ?? roomConfigs.living_room
</script>

<template>
  <RoomShell
    :position="room.position"
    :width="cfg.width"
    :depth="cfg.depth"
    :height="cfg.height"
    :wall-color="cfg.wallColor"
    :floor-color="cfg.floorColor"
  />

  <LivingRoomFurniture v-if="room.type === 'living_room'" :position="room.position" />
  <BedroomFurniture v-else-if="room.type === 'bedroom'" :position="room.position" />
  <KitchenFurniture v-else-if="room.type === 'kitchen'" :position="room.position" />
  <BathroomFurniture v-else-if="room.type === 'bathroom'" :position="room.position" />

  <DeviceVisual
    v-for="[deviceId, devCfg] in deviceEntries"
    :key="deviceId"
    :device-id="deviceId"
    :anchor="[room.position[0] + devCfg.anchor[0], room.position[1] + devCfg.anchor[1], room.position[2] + devCfg.anchor[2]]"
    :device-type="devCfg.type"
  />
</template>
