<script setup lang="ts">
import type { Vec3 } from '@/types/scene-config'

const props = defineProps<{
  position: Vec3
}>()

const px = props.position[0]
const pz = props.position[2]
</script>

<template>
  <!-- ===================== KITCHEN ISLAND (modern, white + wood top) ===================== -->
  <!-- Island body -->
  <TresMesh :position="[px, 0.42, pz - 0.2]" cast-shadow>
    <TresBoxGeometry :args="[1.5, 0.82, 0.65]" />
    <TresMeshStandardMaterial color="#f5f2ee" :roughness="0.6" />
  </TresMesh>
  <!-- Island countertop (stone/quartz) -->
  <TresMesh :position="[px, 0.84, pz - 0.2]" cast-shadow>
    <TresBoxGeometry :args="[1.56, 0.025, 0.72]" />
    <TresMeshStandardMaterial color="#e8e4de" :roughness="0.25" :metalness="0.05" />
  </TresMesh>

  <!-- ===================== BACK COUNTER (L-shape against back + left wall) ===================== -->
  <!-- Back counter body -->
  <TresMesh :position="[px, 0.42, pz - 1.82]" cast-shadow>
    <TresBoxGeometry :args="[3.8, 0.82, 0.6]" />
    <TresMeshStandardMaterial color="#f5f2ee" :roughness="0.6" />
  </TresMesh>
  <!-- Back countertop -->
  <TresMesh :position="[px, 0.84, pz - 1.82]">
    <TresBoxGeometry :args="[3.84, 0.025, 0.65]" />
    <TresMeshStandardMaterial color="#e8e4de" :roughness="0.25" :metalness="0.05" />
  </TresMesh>
  <!-- Backsplash (dark tile) -->
  <TresMesh :position="[px, 1.25, pz - 2.13]">
    <TresBoxGeometry :args="[3.8, 0.8, 0.02]" />
    <TresMeshStandardMaterial color="#d0ccc4" :roughness="0.3" :metalness="0.05" />
  </TresMesh>

  <!-- Left counter body -->
  <TresMesh :position="[px - 1.6, 0.42, pz - 0.85]" cast-shadow>
    <TresBoxGeometry :args="[0.6, 0.82, 1.35]" />
    <TresMeshStandardMaterial color="#f5f2ee" :roughness="0.6" />
  </TresMesh>
  <!-- Left countertop -->
  <TresMesh :position="[px - 1.6, 0.84, pz - 0.85]">
    <TresBoxGeometry :args="[0.65, 0.025, 1.4]" />
    <TresMeshStandardMaterial color="#e8e4de" :roughness="0.25" :metalness="0.05" />
  </TresMesh>

  <!-- ===================== UPPER CABINETS ===================== -->
  <TresMesh :position="[px - 0.8, 2.0, pz - 2.1]" cast-shadow>
    <TresBoxGeometry :args="[1.6, 0.65, 0.32]" />
    <TresMeshStandardMaterial color="#f5f2ee" :roughness="0.55" />
  </TresMesh>
  <TresMesh :position="[px + 0.8, 2.0, pz - 2.1]" cast-shadow>
    <TresBoxGeometry :args="[1.2, 0.65, 0.32]" />
    <TresMeshStandardMaterial color="#f5f2ee" :roughness="0.55" />
  </TresMesh>
  <!-- Cabinet handles -->
  <TresMesh :position="[px - 0.8, 1.9, pz - 1.93]">
    <TresBoxGeometry :args="[0.3, 0.008, 0.005]" />
    <TresMeshStandardMaterial color="#c8a86e" :roughness="0.3" :metalness="0.7" />
  </TresMesh>
  <TresMesh :position="[px + 0.8, 1.9, pz - 1.93]">
    <TresBoxGeometry :args="[0.24, 0.008, 0.005]" />
    <TresMeshStandardMaterial color="#c8a86e" :roughness="0.3" :metalness="0.7" />
  </TresMesh>

  <!-- ===================== SINK (stainless steel) ===================== -->
  <TresMesh :position="[px + 0.5, 0.83, pz - 1.82]">
    <TresBoxGeometry :args="[0.55, 0.03, 0.38]" />
    <TresMeshStandardMaterial color="#c8c4bc" :roughness="0.15" :metalness="0.6" />
  </TresMesh>
  <!-- Faucet -->
  <TresMesh :position="[px + 0.5, 1.0, pz - 1.95]">
    <TresCylinderGeometry :args="[0.012, 0.012, 0.3, 8]" />
    <TresMeshStandardMaterial color="#c8c4bc" :roughness="0.15" :metalness="0.7" />
  </TresMesh>
  <TresMesh :position="[px + 0.5, 1.12, pz - 1.88]" :rotation-x="1.2">
    <TresCylinderGeometry :args="[0.01, 0.01, 0.12, 6]" />
    <TresMeshStandardMaterial color="#c8c4bc" :roughness="0.15" :metalness="0.7" />
  </TresMesh>

  <!-- ===================== STOVE (induction, black glass) ===================== -->
  <TresMesh :position="[px - 0.5, 0.85, pz - 1.82]">
    <TresBoxGeometry :args="[0.6, 0.008, 0.42]" />
    <TresMeshStandardMaterial color="#1a1a1a" :roughness="0.1" :metalness="0.1" />
  </TresMesh>
  <!-- Burner circles -->
  <TresMesh :position="[px - 0.65, 0.856, pz - 1.72]" :rotation-x="-Math.PI / 2">
    <TresRingGeometry :args="[0.06, 0.075, 24]" />
    <TresMeshStandardMaterial color="#333333" :roughness="0.2" />
  </TresMesh>
  <TresMesh :position="[px - 0.35, 0.856, pz - 1.72]" :rotation-x="-Math.PI / 2">
    <TresRingGeometry :args="[0.08, 0.095, 24]" />
    <TresMeshStandardMaterial color="#333333" :roughness="0.2" />
  </TresMesh>

  <!-- ===================== RANGE HOOD ===================== -->
  <TresMesh :position="[px - 0.5, 1.8, pz - 2.0]" cast-shadow>
    <TresBoxGeometry :args="[0.7, 0.15, 0.45]" />
    <TresMeshStandardMaterial color="#d0d0d0" :roughness="0.2" :metalness="0.4" />
  </TresMesh>
  <TresMesh :position="[px - 0.5, 1.65, pz - 2.0]">
    <TresBoxGeometry :args="[0.65, 0.15, 0.4]" />
    <TresMeshStandardMaterial color="#c8c8c8" :roughness="0.2" :metalness="0.4" />
  </TresMesh>

  <!-- ===================== REFRIGERATOR (modern, stainless) ===================== -->
  <TresMesh :position="[px + 1.7, 0.92, pz - 1.5]" cast-shadow>
    <TresBoxGeometry :args="[0.7, 1.8, 0.65]" />
    <TresMeshStandardMaterial color="#e0ddd8" :roughness="0.2" :metalness="0.3" />
  </TresMesh>
  <!-- Fridge door line -->
  <TresMesh :position="[px + 1.35, 0.92, pz - 1.5]">
    <TresBoxGeometry :args="[0.005, 1.7, 0.6]" />
    <TresMeshStandardMaterial color="#c8c4bc" :roughness="0.3" />
  </TresMesh>
  <!-- Handle -->
  <TresMesh :position="[px + 1.34, 1.1, pz - 1.5]">
    <TresBoxGeometry :args="[0.005, 0.4, 0.02]" />
    <TresMeshStandardMaterial color="#c8a86e" :roughness="0.3" :metalness="0.7" />
  </TresMesh>
  <!-- Handle bottom -->
  <TresMesh :position="[px + 1.34, 0.5, pz - 1.5]">
    <TresBoxGeometry :args="[0.005, 0.3, 0.02]" />
    <TresMeshStandardMaterial color="#c8a86e" :roughness="0.3" :metalness="0.7" />
  </TresMesh>

  <!-- ===================== BAR STOOLS (at island) ===================== -->
  <TresMesh v-for="(sx, i) in [-0.35, 0.35]" :key="'stool-' + i"
    :position="[px + sx, 0.36, pz + 0.3]" cast-shadow>
    <TresCylinderGeometry :args="[0.16, 0.16, 0.03, 16]" />
    <TresMeshStandardMaterial color="#404040" :roughness="0.85" />
  </TresMesh>
  <TresMesh v-for="(sx, i) in [-0.35, 0.35]" :key="'stool-leg-' + i"
    :position="[px + sx, 0.18, pz + 0.3]">
    <TresCylinderGeometry :args="[0.015, 0.02, 0.35, 8]" />
    <TresMeshStandardMaterial color="#c8a86e" :roughness="0.3" :metalness="0.7" />
  </TresMesh>
  <TresMesh v-for="(sx, i) in [-0.35, 0.35]" :key="'stool-base-' + i"
    :position="[px + sx, 0.005, pz + 0.3]">
    <TresCylinderGeometry :args="[0.1, 0.1, 0.01, 12]" />
    <TresMeshStandardMaterial color="#c8a86e" :roughness="0.3" :metalness="0.7" />
  </TresMesh>
</template>
