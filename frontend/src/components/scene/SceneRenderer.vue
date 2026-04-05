<script setup lang="ts">
import { computed } from 'vue'
import { TresCanvas } from '@tresjs/core'
import { useWorldStore } from '@/stores/worldStore'
import DeviceMesh from './DeviceMesh.vue'

const worldStore = useWorldStore()

const devices = computed(() => Object.values(worldStore.devices))
</script>

<template>
  <div class="w-full h-full absolute inset-0">
    <TresCanvas>
      <TresOrthographicCamera :position="[12, 12, 12]" :zoom="45" />
      <TresAmbientLight :intensity="0.4" />
      <TresDirectionalLight :position="[5, 10, 5]" :intensity="0.6" />

      <!-- Floor -->
      <TresMesh :position="[0, -0.05, 0]">
        <TresBoxGeometry :args="[10, 0.1, 8]" />
        <TresMeshStandardMaterial color="#1a1a2e" />
      </TresMesh>

      <!-- Left wall -->
      <TresMesh :position="[-5, 1.5, 0]">
        <TresBoxGeometry :args="[0.1, 3, 8]" />
        <TresMeshStandardMaterial color="#16213e" />
      </TresMesh>

      <!-- Right wall -->
      <TresMesh :position="[5, 1.5, 0]">
        <TresBoxGeometry :args="[0.1, 3, 8]" />
        <TresMeshStandardMaterial color="#16213e" />
      </TresMesh>

      <!-- Back wall -->
      <TresMesh :position="[0, 1.5, -4]">
        <TresBoxGeometry :args="[10, 3, 0.1]" />
        <TresMeshStandardMaterial color="#0f3460" />
      </TresMesh>

      <!-- Front wall (transparent) -->
      <TresMesh :position="[0, 1.5, 4]">
        <TresBoxGeometry :args="[10, 3, 0.1]" />
        <TresMeshStandardMaterial color="#1a1a3e" :opacity="0.3" :transparent="true" />
      </TresMesh>

      <!-- Room divider 1 -->
      <TresMesh :position="[-2.5, 1.5, 0]">
        <TresBoxGeometry :args="[0.1, 3, 8]" />
        <TresMeshStandardMaterial color="#1a1a3e" :opacity="0.4" :transparent="true" />
      </TresMesh>

      <!-- Room divider 2 -->
      <TresMesh :position="[2.5, 1.5, 0]">
        <TresBoxGeometry :args="[0.1, 3, 8]" />
        <TresMeshStandardMaterial color="#1a1a3e" :opacity="0.4" :transparent="true" />
      </TresMesh>

      <!-- Furniture: Sofa (living room, bottom-left area) -->
      <TresMesh :position="[-3.8, 0.25, -2.5]">
        <TresBoxGeometry :args="[1.5, 0.5, 0.6]" />
        <TresMeshStandardMaterial color="#2d4a7a" />
      </TresMesh>

      <!-- Furniture: Table (living room center) -->
      <TresMesh :position="[-3.5, 0.2, -1]">
        <TresBoxGeometry :args="[0.8, 0.1, 0.8]" />
        <TresMeshStandardMaterial color="#3a3a4a" />
      </TresMesh>

      <!-- Furniture: Bed (bedroom, bottom-right area) -->
      <TresMesh :position="[3.5, 0.25, -2.5]">
        <TresBoxGeometry :args="[1.4, 0.5, 2.0]" />
        <TresMeshStandardMaterial color="#4a3a5a" />
      </TresMesh>

      <!-- Furniture: Kitchen counter -->
      <TresMesh :position="[-3.5, 0.35, 2.5]">
        <TresBoxGeometry :args="[1.8, 0.7, 0.5]" />
        <TresMeshStandardMaterial color="#3a4a3a" />
      </TresMesh>

      <!-- Devices -->
      <DeviceMesh v-for="dev in devices" :key="dev.id" :device="dev" />
    </TresCanvas>
  </div>
</template>
