<script setup lang="ts">
import type { Vec3 } from '@/types/scene-config'

const props = defineProps<{
  position: Vec3
}>()

const px = props.position[0]
const pz = props.position[2]
</script>

<template>
  <!-- Bed - against back wall -->
  <!-- Bed frame -->
  <TresMesh :position="[px, 0.2, pz - 1.2]">
    <TresBoxGeometry :args="[2.0, 0.3, 1.8]" />
    <TresMeshStandardMaterial color="#c4a882" roughness="0.7" />
  </TresMesh>
  <!-- Headboard -->
  <TresMesh :position="[px, 0.8, pz - 2.05]">
    <TresBoxGeometry :args="[2.0, 0.9, 0.1]" />
    <TresMeshStandardMaterial color="#b89e78" roughness="0.8" />
  </TresMesh>
  <!-- Mattress -->
  <TresMesh :position="[px, 0.4, pz - 1.15]">
    <TresBoxGeometry :args="[1.8, 0.15, 1.6]" />
    <TresMeshStandardMaterial color="#f0ebe4" roughness="0.95" />
  </TresMesh>
  <!-- Pillow left -->
  <TresMesh :position="[px - 0.4, 0.52, pz - 1.8]">
    <TresBoxGeometry :args="[0.5, 0.1, 0.35]" />
    <TresMeshStandardMaterial color="#e8e0d4" roughness="0.95" />
  </TresMesh>
  <!-- Pillow right -->
  <TresMesh :position="[px + 0.4, 0.52, pz - 1.8]">
    <TresBoxGeometry :args="[0.5, 0.1, 0.35]" />
    <TresMeshStandardMaterial color="#e8e0d4" roughness="0.95" />
  </TresMesh>
  <!-- Blanket -->
  <TresMesh :position="[px, 0.5, pz - 0.7]">
    <TresBoxGeometry :args="[1.7, 0.06, 1.0]" />
    <TresMeshStandardMaterial color="#8ab4c8" roughness="0.9" />
  </TresMesh>

  <!-- Nightstand left -->
  <TresMesh :position="[px - 1.3, 0.25, pz - 1.8]">
    <TresBoxGeometry :args="[0.45, 0.45, 0.4]" />
    <TresMeshStandardMaterial color="#b8a082" roughness="0.7" />
  </TresMesh>
  <!-- Lamp on nightstand -->
  <TresMesh :position="[px - 1.3, 0.52, pz - 1.8]">
    <TresCylinderGeometry :args="[0.06, 0.08, 0.04, 10]" />
    <TresMeshStandardMaterial color="#ddd" roughness="0.4" metalness="0.3" />
  </TresMesh>
  <TresMesh :position="[px - 1.3, 0.7, pz - 1.8]">
    <TresCylinderGeometry :args="[0.01, 0.01, 0.3, 6]" />
    <TresMeshStandardMaterial color="#aaa" roughness="0.4" metalness="0.4" />
  </TresMesh>
  <TresMesh :position="[px - 1.3, 0.88, pz - 1.8]">
    <TresCylinderGeometry :args="[0.12, 0.02, 0.16, 10]" />
    <TresMeshStandardMaterial color="#f5e6c8" roughness="0.8" />
  </TresMesh>

  <!-- Nightstand right -->
  <TresMesh :position="[px + 1.3, 0.25, pz - 1.8]">
    <TresBoxGeometry :args="[0.45, 0.45, 0.4]" />
    <TresMeshStandardMaterial color="#b8a082" roughness="0.7" />
  </TresMesh>

  <!-- Wardrobe against left wall -->
  <TresMesh :position="[px - 2.2, 1.2, pz]">
    <TresBoxGeometry :args="[0.5, 2.2, 1.2]" />
    <TresMeshStandardMaterial color="#a89478" roughness="0.7" />
  </TresMesh>
  <!-- Wardrobe doors line -->
  <TresMesh :position="[px - 1.94, 1.2, pz]">
    <TresBoxGeometry :args="[0.01, 2.0, 0.01]" />
    <TresMeshStandardMaterial color="#8a7e68" roughness="0.8" />
  </TresMesh>

  <!-- Window on back wall -->
  <TresMesh :position="[px, 1.5, pz - 2.47]">
    <TresBoxGeometry :args="[1.0, 1.0, 0.04]" />
    <TresMeshStandardMaterial color="#b8d4e8" roughness="0.1" transparent opacity="0.3" />
  </TresMesh>
  <TresMesh :position="[px, 1.5, pz - 2.46]">
    <TresBoxGeometry :args="[1.08, 1.08, 0.02]" />
    <TresMeshStandardMaterial color="#e8e0d0" roughness="0.7" />
  </TresMesh>

  <!-- Rug next to bed -->
  <TresMesh :position="[px + 0.5, 0.03, pz + 0.2]" rotation-x="-Math.PI / 2">
    <TresPlaneGeometry :args="[1.2, 0.8]" />
    <TresMeshStandardMaterial color="#d4c0a0" roughness="0.95" />
  </TresMesh>
</template>
