<script setup lang="ts">
import type { Vec3 } from '@/types/scene-config'

const props = defineProps<{
  position: Vec3
}>()

const px = props.position[0]
const pz = props.position[2]
</script>

<template>
  <!-- Sofa - L-shaped, against back wall -->
  <!-- Sofa seat -->
  <TresMesh :position="[px - 0.3, 0.35, pz - 1.6]">
    <TresBoxGeometry :args="[2.4, 0.35, 0.9]" />
    <TresMeshStandardMaterial color="#7a9eb8" roughness="0.85" />
  </TresMesh>
  <!-- Sofa back -->
  <TresMesh :position="[px - 0.3, 0.7, pz - 2.0]">
    <TresBoxGeometry :args="[2.4, 0.55, 0.15]" />
    <TresMeshStandardMaterial color="#6b8faa" roughness="0.85" />
  </TresMesh>
  <!-- Sofa left arm -->
  <TresMesh :position="[px - 1.5, 0.5, pz - 1.6]">
    <TresBoxGeometry :args="[0.15, 0.45, 0.9]" />
    <TresMeshStandardMaterial color="#6b8faa" roughness="0.85" />
  </TresMesh>
  <!-- Sofa right arm -->
  <TresMesh :position="[px + 0.9, 0.5, pz - 1.6]">
    <TresBoxGeometry :args="[0.15, 0.45, 0.9]" />
    <TresMeshStandardMaterial color="#6b8faa" roughness="0.85" />
  </TresMesh>
  <!-- Sofa cushions -->
  <TresMesh :position="[px - 0.6, 0.45, pz - 1.5]">
    <TresBoxGeometry :args="[0.6, 0.12, 0.6]" />
    <TresMeshStandardMaterial color="#8aafcc" roughness="0.9" />
  </TresMesh>
  <TresMesh :position="[px + 0.3, 0.45, pz - 1.5]">
    <TresBoxGeometry :args="[0.6, 0.12, 0.6]" />
    <TresMeshStandardMaterial color="#8aafcc" roughness="0.9" />
  </TresMesh>

  <!-- Coffee table -->
  <TresMesh :position="[px - 0.3, 0.25, pz - 0.6]">
    <TresBoxGeometry :args="[1.0, 0.04, 0.5]" />
    <TresMeshStandardMaterial color="#a0845c" roughness="0.5" />
  </TresMesh>
  <!-- Coffee table legs -->
  <TresMesh :position="[px - 0.7, 0.1, pz - 0.8]">
    <TresBoxGeometry :args="[0.04, 0.2, 0.04]" />
    <TresMeshStandardMaterial color="#8a7048" roughness="0.6" />
  </TresMesh>
  <TresMesh :position="[px + 0.1, 0.1, pz - 0.8]">
    <TresBoxGeometry :args="[0.04, 0.2, 0.04]" />
    <TresMeshStandardMaterial color="#8a7048" roughness="0.6" />
  </TresMesh>
  <TresMesh :position="[px - 0.7, 0.1, pz - 0.4]">
    <TresBoxGeometry :args="[0.04, 0.2, 0.04]" />
    <TresMeshStandardMaterial color="#8a7048" roughness="0.6" />
  </TresMesh>
  <TresMesh :position="[px + 0.1, 0.1, pz - 0.4]">
    <TresBoxGeometry :args="[0.04, 0.2, 0.04]" />
    <TresMeshStandardMaterial color="#8a7048" roughness="0.6" />
  </TresMesh>

  <!-- TV cabinet -->
  <TresMesh :position="[px - 0.3, 0.25, pz + 1.8]">
    <TresBoxGeometry :args="[2.2, 0.4, 0.45]" />
    <TresMeshStandardMaterial color="#b8a88a" roughness="0.7" />
  </TresMesh>
  <!-- TV stand base -->
  <TresMesh :position="[px - 0.3, 0.48, pz + 1.6]">
    <TresBoxGeometry :args="[0.4, 0.02, 0.15]" />
    <TresMeshStandardMaterial color="#333" roughness="0.3" metalness="0.6" />
  </TresMesh>
  <!-- TV screen -->
  <TresMesh :position="[px - 0.3, 1.1, pz + 1.95]">
    <TresBoxGeometry :args="[1.4, 0.85, 0.04]" />
    <TresMeshStandardMaterial color="#1a1a2e" roughness="0.2" metalness="0.3" />
  </TresMesh>
  <!-- TV frame -->
  <TresMesh :position="[px - 0.3, 1.1, pz + 1.93]">
    <TresBoxGeometry :args="[1.44, 0.89, 0.02]" />
    <TresMeshStandardMaterial color="#222" roughness="0.4" metalness="0.5" />
  </TresMesh>

  <!-- Rug under coffee table -->
  <TresMesh :position="[px - 0.3, 0.03, pz - 0.8]" rotation-x="-Math.PI / 2">
    <TresPlaneGeometry :args="[2.0, 1.4]" />
    <TresMeshStandardMaterial color="#c9b896" roughness="0.95" />
  </TresMesh>

  <!-- Floor lamp next to sofa -->
  <TresMesh :position="[px - 1.7, 0.05, pz - 1.0]">
    <TresCylinderGeometry :args="[0.08, 0.12, 0.02, 12]" />
    <TresMeshStandardMaterial color="#444" roughness="0.5" metalness="0.4" />
  </TresMesh>
  <TresMesh :position="[px - 1.7, 0.8, pz - 1.0]">
    <TresCylinderGeometry :args="[0.02, 0.02, 1.4, 8]" />
    <TresMeshStandardMaterial color="#444" roughness="0.5" metalness="0.4" />
  </TresMesh>
  <TresMesh :position="[px - 1.7, 1.55, pz - 1.0]">
    <TresCylinderGeometry :args="[0.15, 0.02, 0.2, 12]" />
    <TresMeshStandardMaterial color="#f5e6c8" roughness="0.8" />
  </TresMesh>

  <!-- Window on back wall -->
  <TresMesh :position="[px + 0.8, 1.5, pz - 2.47]">
    <TresBoxGeometry :args="[1.2, 1.2, 0.04]" />
    <TresMeshStandardMaterial color="#b8d4e8" roughness="0.1" transparent opacity="0.3" />
  </TresMesh>
  <!-- Window frame -->
  <TresMesh :position="[px + 0.8, 1.5, pz - 2.46]">
    <TresBoxGeometry :args="[1.28, 1.28, 0.02]" />
    <TresMeshStandardMaterial color="#e8e0d0" roughness="0.7" />
  </TresMesh>
</template>
