<script setup lang="ts">
import { useGLTF } from '@tresjs/cientos'
import type { RoomConfig } from '@/types/scene-config'
import DeviceVisual from './DeviceVisual.vue'

const props = defineProps<{
  room: RoomConfig
}>()

let scene: any = null
let loadError = false

try {
  const { execute } = useGLTF(props.room.model, { draco: false })
  const gltf = await execute()
  scene = gltf.scene
} catch {
  loadError = true
}

const deviceEntries = Object.entries(props.room.devices)
</script>

<template>
  <!-- Real GLB model -->
  <primitive
    v-if="scene"
    :object="scene"
    :position="room.position"
    :rotation="room.rotation"
  />

  <!-- Fallback: procedural room box when GLB fails to load -->
  <template v-if="loadError">
    <!-- Room walls -->
    <TresMesh :position="room.position">
      <TresBoxGeometry :args="[4, 3, 4]" />
      <TresMeshStandardMaterial color="#3a3a5e" />
    </TresMesh>
    <!-- Floor -->
    <TresMesh :position="[room.position[0], -0.05, room.position[2]]">
      <TresBoxGeometry :args="[4, 0.1, 4]" />
      <TresMeshStandardMaterial color="#2a2a4e" />
    </TresMesh>
  </template>

  <!-- Devices at world-space anchors -->
  <DeviceVisual
    v-for="[deviceId, cfg] in deviceEntries"
    :key="deviceId"
    :device-id="deviceId"
    :anchor="[room.position[0] + cfg.anchor[0], room.position[1] + cfg.anchor[1], room.position[2] + cfg.anchor[2]]"
    :device-type="cfg.type"
  />
</template>
