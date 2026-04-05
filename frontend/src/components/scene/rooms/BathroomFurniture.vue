<script setup lang="ts">
import type { Vec3 } from '@/types/scene-config'

const props = defineProps<{
  position: Vec3
}>()

const px = props.position[0]
const pz = props.position[2]
</script>

<template>
  <!-- Shower area - glass partition against back-left corner -->
  <!-- Shower tray -->
  <TresMesh :position="[px - 1.0, 0.04, pz - 1.3]" receive-shadow>
    <TresBoxGeometry :args="[1.2, 0.04, 1.2]" />
    <TresMeshStandardMaterial color="#d0ccc4" roughness="0.3" />
  </TresMesh>
  <!-- Shower glass wall (left) -->
  <TresMesh :position="[px - 1.6, 1.1, pz - 1.3]">
    <TresBoxGeometry :args="[0.02, 2.0, 1.2]" />
    <TresMeshStandardMaterial color="#d8e8f0" roughness="0.1" transparent opacity="0.25" />
  </TresMesh>
  <!-- Shower glass wall (back) -->
  <TresMesh :position="[px - 1.0, 1.1, pz - 1.9]">
    <TresBoxGeometry :args="[1.2, 2.0, 0.02]" />
    <TresMeshStandardMaterial color="#d8e8f0" roughness="0.1" transparent opacity="0.25" />
  </TresMesh>
  <!-- Shower head -->
  <TresMesh :position="[px - 1.0, 2.2, pz - 1.3]">
    <TresCylinderGeometry :args="[0.12, 0.12, 0.03, 12]" />
    <TresMeshStandardMaterial color="#ccc" roughness="0.3" metalness="0.5" />
  </TresMesh>

  <!-- Sink/vanity against right wall -->
  <TresMesh :position="[px + 1.3, 0.42, pz - 0.3]">
    <TresBoxGeometry :args="[0.8, 0.8, 0.5]" />
    <TresMeshStandardMaterial color="#e8e0d4" roughness="0.6" />
  </TresMesh>
  <!-- Sink basin -->
  <TresMesh :position="[px + 1.3, 0.83, pz - 0.3]">
    <TresCylinderGeometry :args="[0.2, 0.18, 0.08, 16]" />
    <TresMeshStandardMaterial color="#d0ccc4" roughness="0.2" />
  </TresMesh>
  <!-- Mirror above sink -->
  <TresMesh :position="[px + 1.98, 1.4, pz - 0.3]">
    <TresBoxGeometry :args="[0.02, 0.8, 0.6]" />
    <TresMeshStandardMaterial color="#a8c8d8" roughness="0.05" metalness="0.8" />
  </TresMesh>
  <!-- Mirror frame -->
  <TresMesh :position="[px + 1.97, 1.4, pz - 0.3]">
    <TresBoxGeometry :args="[0.02, 0.84, 0.64]" />
    <TresMeshStandardMaterial color="#c8b898" roughness="0.7" />
  </TresMesh>

  <!-- Toilet against left wall -->
  <TresMesh :position="[px - 1.6, 0.2, pz + 1.0]">
    <TresBoxGeometry :args="[0.45, 0.35, 0.55]" />
    <TresMeshStandardMaterial color="#f0ece8" roughness="0.4" />
  </TresMesh>
  <!-- Toilet tank -->
  <TresMesh :position="[px - 1.6, 0.45, pz + 1.25]">
    <TresBoxGeometry :args="[0.4, 0.5, 0.15]" />
    <TresMeshStandardMaterial color="#f0ece8" roughness="0.4" />
  </TresMesh>
  <!-- Toilet lid -->
  <TresMesh :position="[px - 1.6, 0.38, pz + 0.85]">
    <TresBoxGeometry :args="[0.4, 0.03, 0.4]" />
    <TresMeshStandardMaterial color="#e8e4e0" roughness="0.5" />
  </TresMesh>

  <!-- Bath mat -->
  <TresMesh :position="[px - 0.3, 0.03, pz + 0.5]" rotation-x="-Math.PI / 2">
    <TresPlaneGeometry :args="[0.6, 0.9]" />
    <TresMeshStandardMaterial color="#b8c8c0" roughness="0.95" />
  </TresMesh>

  <!-- Small shelf on back wall -->
  <TresMesh :position="[px + 0.5, 1.3, pz - 2.2]">
    <TresBoxGeometry :args="[0.6, 0.04, 0.2]" />
    <TresMeshStandardMaterial color="#c8b898" roughness="0.7" />
  </TresMesh>
</template>
