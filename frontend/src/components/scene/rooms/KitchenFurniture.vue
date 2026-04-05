<script setup lang="ts">
import type { Vec3 } from '@/types/scene-config'

const props = defineProps<{
  position: Vec3
}>()

const px = props.position[0]
const pz = props.position[2]
</script>

<template>
  <!-- Kitchen counter - L-shaped along back + left wall -->
  <!-- Back counter -->
  <TresMesh :position="[px, 0.45, pz - 1.8]">
    <TresBoxGeometry :args="[3.6, 0.85, 0.65]" />
    <TresMeshStandardMaterial color="#e0d8cc" roughness="0.6" />
  </TresMesh>
  <!-- Back counter top -->
  <TresMesh :position="[px, 0.88, pz - 1.8]">
    <TresBoxGeometry :args="[3.64, 0.03, 0.69]" />
    <TresMeshStandardMaterial color="#c8c0b4" roughness="0.4" />
  </TresMesh>

  <!-- Left counter (perpendicular) -->
  <TresMesh :position="[px - 1.5, 0.45, pz - 0.5]">
    <TresBoxGeometry :args="[0.65, 0.85, 1.8]" />
    <TresMeshStandardMaterial color="#e0d8cc" roughness="0.6" />
  </TresMesh>
  <!-- Left counter top -->
  <TresMesh :position="[px - 1.5, 0.88, pz - 0.5]">
    <TresBoxGeometry :args="[0.69, 0.03, 1.84]" />
    <TresMeshStandardMaterial color="#c8c0b4" roughness="0.4" />
  </TresMesh>

  <!-- Upper cabinets - back wall -->
  <TresMesh :position="[px - 0.5, 2.0, pz - 2.15]">
    <TresBoxGeometry :args="[1.8, 0.7, 0.35]" />
    <TresMeshStandardMaterial color="#d8d0c4" roughness="0.7" />
  </TresMesh>
  <TresMesh :position="[px + 1.0, 2.0, pz - 2.15]">
    <TresBoxGeometry :args="[1.2, 0.7, 0.35]" />
    <TresMeshStandardMaterial color="#d8d0c4" roughness="0.7" />
  </TresMesh>

  <!-- Sink (depression in counter) -->
  <TresMesh :position="[px + 0.4, 0.87, pz - 1.8]">
    <TresBoxGeometry :args="[0.5, 0.04, 0.4]" />
    <TresMeshStandardMaterial color="#a8a098" roughness="0.2" metalness="0.4" />
  </TresMesh>

  <!-- Stove (dark area on counter) -->
  <TresMesh :position="[px - 0.5, 0.89, pz - 1.8]">
    <TresBoxGeometry :args="[0.55, 0.01, 0.45]" />
    <TresMeshStandardMaterial color="#333" roughness="0.3" metalness="0.3" />
  </TresMesh>
  <!-- Stove burners -->
  <TresMesh :position="[px - 0.65, 0.90, pz - 1.7]" rotation-x="-Math.PI / 2">
    <TresRingGeometry :args="[0.06, 0.08, 16]" />
    <TresMeshStandardMaterial color="#555" roughness="0.4" />
  </TresMesh>
  <TresMesh :position="[px - 0.35, 0.90, pz - 1.7]" rotation-x="-Math.PI / 2">
    <TresRingGeometry :args="[0.06, 0.08, 16]" />
    <TresMeshStandardMaterial color="#555" roughness="0.4" />
  </TresMesh>

  <!-- Refrigerator against right wall -->
  <TresMesh :position="[px + 1.8, 0.9, pz - 1.3]">
    <TresBoxGeometry :args="[0.7, 1.7, 0.65]" />
    <TresMeshStandardMaterial color="#d0d0d0" roughness="0.3" metalness="0.2" />
  </TresMesh>
  <!-- Fridge handle -->
  <TresMesh :position="[px + 1.44, 1.0, pz - 1.3]">
    <TresBoxGeometry :args="[0.02, 0.3, 0.04]" />
    <TresMeshStandardMaterial color="#888" roughness="0.3" metalness="0.5" />
  </TresMesh>

  <!-- Small dining table -->
  <TresMesh :position="[px + 0.5, 0.38, pz + 0.8]">
    <TresBoxGeometry :args="[0.9, 0.04, 0.9]" />
    <TresMeshStandardMaterial color="#b8a082" roughness="0.6" />
  </TresMesh>
  <!-- Table legs -->
  <TresMesh :position="[px + 0.15, 0.17, pz + 0.45]">
    <TresBoxGeometry :args="[0.04, 0.32, 0.04]" />
    <TresMeshStandardMaterial color="#9a8468" roughness="0.6" />
  </TresMesh>
  <TresMesh :position="[px + 0.85, 0.17, pz + 0.45]">
    <TresBoxGeometry :args="[0.04, 0.32, 0.04]" />
    <TresMeshStandardMaterial color="#9a8468" roughness="0.6" />
  </TresMesh>
  <TresMesh :position="[px + 0.15, 0.17, pz + 1.15]">
    <TresBoxGeometry :args="[0.04, 0.32, 0.04]" />
    <TresMeshStandardMaterial color="#9a8468" roughness="0.6" />
  </TresMesh>
  <TresMesh :position="[px + 0.85, 0.17, pz + 1.15]">
    <TresBoxGeometry :args="[0.04, 0.32, 0.04]" />
    <TresMeshStandardMaterial color="#9a8468" roughness="0.6" />
  </TresMesh>
</template>
