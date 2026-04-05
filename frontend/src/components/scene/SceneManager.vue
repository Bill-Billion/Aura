<script setup lang="ts">
import { useSceneConfig } from '@/composables/useSceneConfig'
import RoomModule from './RoomModule.vue'

const { config, loading, error } = useSceneConfig()
</script>

<template>
  <!-- Loading state -->
  <TresMesh v-if="loading" :position="[0, 1.5, 0]">
    <TresBoxGeometry :args="[1, 1, 1]" />
    <TresMeshStandardMaterial color="#666" :emissive="'#333'" :emissive-intensity="0.5" />
  </TresMesh>

  <!-- Error state -->
  <TresMesh v-else-if="error" :position="[0, 1.5, 0]">
    <TresBoxGeometry :args="[2, 0.5, 0.1]" />
    <TresMeshStandardMaterial color="#ff4444" />
  </TresMesh>

  <!-- Loaded: render rooms -->
  <template v-else-if="config">
    <RoomModule
      v-for="room in config.rooms"
      :key="room.id"
      :room="room"
    />
  </template>
</template>
