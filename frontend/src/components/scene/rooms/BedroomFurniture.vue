<script setup lang="ts">
import type { Vec3 } from '@/types/scene-config'

const props = defineProps<{
  position: Vec3
}>()

const px = props.position[0]
const pz = props.position[2]
</script>

<template>
  <!-- ===================== BED (modern platform bed, light wood + white) ===================== -->
  <!-- Platform base -->
  <TresMesh :position="[px, 0.15, pz - 1.2]" cast-shadow>
    <TresBoxGeometry :args="[2.1, 0.22, 2.0]" />
    <TresMeshStandardMaterial color="#c4a67a" :roughness="0.55" :metalness="0.0" />
  </TresMesh>
  <!-- Headboard (fabric, dark gray) -->
  <TresMesh :position="[px, 0.85, pz - 2.18]" cast-shadow>
    <TresBoxGeometry :args="[2.1, 1.1, 0.08]" />
    <TresMeshStandardMaterial color="#444444" :roughness="0.92" />
  </TresMesh>
  <!-- Headboard padding panels -->
  <TresMesh :position="[px - 0.5, 0.8, pz - 2.13]">
    <TresBoxGeometry :args="[0.55, 0.7, 0.04]" />
    <TresMeshStandardMaterial color="#4d4d4d" :roughness="0.95" />
  </TresMesh>
  <TresMesh :position="[px + 0.5, 0.8, pz - 2.13]">
    <TresBoxGeometry :args="[0.55, 0.7, 0.04]" />
    <TresMeshStandardMaterial color="#4d4d4d" :roughness="0.95" />
  </TresMesh>

  <!-- Mattress -->
  <TresMesh :position="[px, 0.34, pz - 1.15]" cast-shadow>
    <TresBoxGeometry :args="[1.9, 0.16, 1.8]" />
    <TresMeshStandardMaterial color="#f5f2ee" :roughness="0.95" />
  </TresMesh>
  <!-- Duvet (folded) -->
  <TresMesh :position="[px, 0.44, pz - 0.7]" cast-shadow>
    <TresBoxGeometry :args="[1.8, 0.08, 1.1]" />
    <TresMeshStandardMaterial color="#e8e4de" :roughness="0.92" />
  </TresMesh>
  <!-- Duvet fold -->
  <TresMesh :position="[px, 0.48, pz - 0.14]">
    <TresBoxGeometry :args="[1.8, 0.04, 0.06]" />
    <TresMeshStandardMaterial color="#ddd8d0" :roughness="0.9" />
  </TresMesh>

  <!-- Pillows -->
  <TresMesh :position="[px - 0.42, 0.48, pz - 1.88]" :rotation-z="0.08" cast-shadow>
    <TresBoxGeometry :args="[0.52, 0.12, 0.36]" />
    <TresMeshStandardMaterial color="#f0ece6" :roughness="0.95" />
  </TresMesh>
  <TresMesh :position="[px + 0.42, 0.48, pz - 1.88]" :rotation-z="-0.05" cast-shadow>
    <TresBoxGeometry :args="[0.52, 0.12, 0.36]" />
    <TresMeshStandardMaterial color="#f0ece6" :roughness="0.95" />
  </TresMesh>
  <!-- Accent pillow -->
  <TresMesh :position="[px, 0.50, pz - 1.65]" :rotation-z="0.1" cast-shadow>
    <TresBoxGeometry :args="[0.3, 0.3, 0.06]" />
    <TresMeshStandardMaterial color="#7a9e8a" :roughness="0.9" />
  </TresMesh>

  <!-- ===================== NIGHTSTANDS (modern, white + wood) ===================== -->
  <!-- Left nightstand -->
  <TresMesh :position="[px - 1.35, 0.24, pz - 1.9]" cast-shadow>
    <TresBoxGeometry :args="[0.48, 0.42, 0.38]" />
    <TresMeshStandardMaterial color="#f5f0e8" :roughness="0.6" />
  </TresMesh>
  <!-- Nightstand top (wood) -->
  <TresMesh :position="[px - 1.35, 0.46, pz - 1.9]">
    <TresBoxGeometry :args="[0.5, 0.02, 0.4]" />
    <TresMeshStandardMaterial color="#b89b72" :roughness="0.5" />
  </TresMesh>
  <!-- Handle -->
  <TresMesh :position="[px - 1.35, 0.24, pz - 1.7]">
    <TresBoxGeometry :args="[0.14, 0.008, 0.005]" />
    <TresMeshStandardMaterial color="#c8a86e" :roughness="0.3" :metalness="0.7" />
  </TresMesh>

  <!-- Right nightstand -->
  <TresMesh :position="[px + 1.35, 0.24, pz - 1.9]" cast-shadow>
    <TresBoxGeometry :args="[0.48, 0.42, 0.38]" />
    <TresMeshStandardMaterial color="#f5f0e8" :roughness="0.6" />
  </TresMesh>
  <TresMesh :position="[px + 1.35, 0.46, pz - 1.9]">
    <TresBoxGeometry :args="[0.5, 0.02, 0.4]" />
    <TresMeshStandardMaterial color="#b89b72" :roughness="0.5" />
  </TresMesh>
  <TresMesh :position="[px + 1.35, 0.24, pz - 1.7]">
    <TresBoxGeometry :args="[0.14, 0.008, 0.005]" />
    <TresMeshStandardMaterial color="#c8a86e" :roughness="0.3" :metalness="0.7" />
  </TresMesh>

  <!-- Bedside lamp (modern, metal + fabric) -->
  <TresMesh :position="[px - 1.35, 0.5, pz - 1.9]">
    <TresCylinderGeometry :args="[0.06, 0.06, 0.02, 12]" />
    <TresMeshStandardMaterial color="#c8a86e" :roughness="0.3" :metalness="0.7" />
  </TresMesh>
  <TresMesh :position="[px - 1.35, 0.65, pz - 1.9]">
    <TresCylinderGeometry :args="[0.008, 0.008, 0.28, 6]" />
    <TresMeshStandardMaterial color="#c8a86e" :roughness="0.3" :metalness="0.7" />
  </TresMesh>
  <TresMesh :position="[px - 1.35, 0.82, pz - 1.9]">
    <TresCylinderGeometry :args="[0.1, 0.06, 0.16, 16]" />
    <TresMeshStandardMaterial color="#f0e0c0" :roughness="0.8" />
  </TresMesh>

  <!-- ===================== WARDROBE (built-in, sliding doors) ===================== -->
  <TresMesh :position="[px - 2.15, 1.3, pz + 0.2]" cast-shadow>
    <TresBoxGeometry :args="[0.55, 2.4, 1.8]" />
    <TresMeshStandardMaterial color="#f0ece4" :roughness="0.6" />
  </TresMesh>
  <!-- Door panels -->
  <TresMesh :position="[px - 1.87, 1.3, pz - 0.24]">
    <TresBoxGeometry :args="[0.01, 2.3, 0.86]" />
    <TresMeshStandardMaterial color="#e8e4dc" :roughness="0.55" />
  </TresMesh>
  <TresMesh :position="[px - 1.87, 1.3, pz + 0.64]">
    <TresBoxGeometry :args="[0.01, 2.3, 0.86]" />
    <TresMeshStandardMaterial color="#eae6de" :roughness="0.55" />
  </TresMesh>
  <!-- Door handles -->
  <TresMesh :position="[px - 1.86, 1.3, pz + 0.2]">
    <TresBoxGeometry :args="[0.005, 0.5, 0.015]" />
    <TresMeshStandardMaterial color="#c8a86e" :roughness="0.3" :metalness="0.7" />
  </TresMesh>

  <!-- ===================== RUG ===================== -->
  <TresMesh :position="[px + 0.3, 0.03, pz + 0.5]" :rotation-x="-Math.PI / 2">
    <TresPlaneGeometry :args="[1.5, 1.0]" />
    <TresMeshStandardMaterial color="#c8b898" :roughness="0.98" />
  </TresMesh>

  <!-- ===================== WINDOW ===================== -->
  <TresMesh :position="[px, 1.6, pz - 2.47]">
    <TresBoxGeometry :args="[1.4, 1.4, 0.02]" />
    <TresMeshStandardMaterial color="#c8dce8" :roughness="0.05" :metalness="0.1" :transparent="true" :opacity="0.2" />
  </TresMesh>
  <TresMesh :position="[px, 1.6, pz - 2.46]">
    <TresBoxGeometry :args="[1.46, 1.46, 0.015]" />
    <TresMeshStandardMaterial color="#ffffff" :roughness="0.5" />
  </TresMesh>
  <TresMesh :position="[px, 1.6, pz - 2.455]">
    <TresBoxGeometry :args="[0.015, 1.4, 0.008]" />
    <TresMeshStandardMaterial color="#ffffff" :roughness="0.5" />
  </TresMesh>
</template>
