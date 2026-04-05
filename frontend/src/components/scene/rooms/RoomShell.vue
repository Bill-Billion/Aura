<script setup lang="ts">
import type { Vec3 } from '@/types/scene-config'

const props = withDefaults(defineProps<{
  position: Vec3
  width?: number
  depth?: number
  height?: number
  wallColor?: string
  floorColor?: string
}>(), {
  width: 5,
  depth: 5,
  height: 3,
  wallColor: '#f0ece4',
  floorColor: '#d4c4a8',
})

const w = props.width
const d = props.depth
const h = props.height
const px = props.position[0]
const pz = props.position[2]
const wt = 0.06
</script>

<template>
  <!-- Floor -->
  <TresMesh :position="[px, 0, pz]" receive-shadow>
    <TresBoxGeometry :args="[w, 0.05, d]" />
    <TresMeshStandardMaterial :color="floorColor" roughness="0.8" />
  </TresMesh>

  <!-- Back wall (full height) -->
  <TresMesh :position="[px, h / 2, pz - d / 2 + wt / 2]">
    <TresBoxGeometry :args="[w, h, wt]" />
    <TresMeshStandardMaterial :color="wallColor" roughness="0.9" />
  </TresMesh>

  <!-- Left wall (full height) -->
  <TresMesh :position="[px - w / 2 + wt / 2, h / 2, pz]">
    <TresBoxGeometry :args="[wt, h, d]" />
    <TresMeshStandardMaterial :color="wallColor" roughness="0.9" />
  </TresMesh>

  <!-- Right wall (half height - gives open feel) -->
  <TresMesh :position="[px + w / 2 - wt / 2, h * 0.25, pz]">
    <TresBoxGeometry :args="[wt, h * 0.5, d]" />
    <TresMeshStandardMaterial :color="wallColor" roughness="0.9" />
  </TresMesh>

  <!-- No front wall, no ceiling - open dollhouse view -->

  <!-- Baseboard - back -->
  <TresMesh :position="[px, 0.06, pz - d / 2 + 0.04]">
    <TresBoxGeometry :args="[w, 0.1, 0.03]" />
    <TresMeshStandardMaterial color="#c8b896" roughness="0.7" />
  </TresMesh>

  <!-- Baseboard - left -->
  <TresMesh :position="[px - w / 2 + 0.04, 0.06, pz]">
    <TresBoxGeometry :args="[0.03, 0.1, d]" />
    <TresMeshStandardMaterial color="#c8b896" roughness="0.7" />
  </TresMesh>

  <!-- Baseboard - right -->
  <TresMesh :position="[px + w / 2 - 0.04, 0.06, pz]">
    <TresBoxGeometry :args="[0.03, 0.1, d]" />
    <TresMeshStandardMaterial color="#c8b896" roughness="0.7" />
  </TresMesh>
</template>
