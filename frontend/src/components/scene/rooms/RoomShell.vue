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
  wallColor: '#f5f5f0',
  floorColor: '#c4a67a',
})

const w = props.width
const d = props.depth
const h = props.height
const px = props.position[0]
const pz = props.position[2]
const wt = 0.08 // wall thickness
const bt = 0.06 // baseboard thickness
const bh = 0.08 // baseboard height
</script>

<template>
  <!-- ========== FLOOR ========== -->
  <!-- Main floor with wood-like appearance -->
  <TresMesh :position="[px, 0, pz]" receive-shadow>
    <TresBoxGeometry :args="[w, 0.05, d]" />
    <TresMeshStandardMaterial :color="floorColor" :roughness="0.65" :metalness="0.0" />
  </TresMesh>
  <!-- Floor planks effect (subtle lines) -->
  <TresMesh v-for="i in 8" :key="'plank-' + i" :position="[px - w/2 + (w / 8) * (i - 0.5), 0.028, pz]">
    <TresBoxGeometry :args="[0.006, 0.001, d - 0.1]" />
    <TresMeshStandardMaterial color="#9e8560" :roughness="0.7" />
  </TresMesh>

  <!-- ========== WALLS ========== -->
  <!-- Back wall (full height, smooth white) -->
  <TresMesh :position="[px, h / 2, pz - d / 2 + wt / 2]" cast-shadow receive-shadow>
    <TresBoxGeometry :args="[w, h, wt]" />
    <TresMeshStandardMaterial :color="wallColor" :roughness="0.92" :metalness="0.0" />
  </TresMesh>

  <!-- Left wall (full height) -->
  <TresMesh :position="[px - w / 2 + wt / 2, h / 2, pz]" cast-shadow receive-shadow>
    <TresBoxGeometry :args="[wt, h, d]" />
    <TresMeshStandardMaterial :color="wallColor" :roughness="0.92" :metalness="0.0" />
  </TresMesh>

  <!-- Right wall (40% height - open feel for camera view) -->
  <TresMesh :position="[px + w / 2 - wt / 2, h * 0.2, pz]">
    <TresBoxGeometry :args="[wt, h * 0.4, d]" />
    <TresMeshStandardMaterial :color="wallColor" :roughness="0.92" :metalness="0.0" />
  </TresMesh>

  <!-- ========== BASEBOARDS (white, modern) ========== -->
  <!-- Back baseboard -->
  <TresMesh :position="[px, bh / 2, pz - d / 2 + wt + bt / 2]">
    <TresBoxGeometry :args="[w - wt * 2, bh, bt]" />
    <TresMeshStandardMaterial color="#ffffff" :roughness="0.5" />
  </TresMesh>
  <!-- Left baseboard -->
  <TresMesh :position="[px - w / 2 + wt + bt / 2, bh / 2, pz]">
    <TresBoxGeometry :args="[bt, bh, d - wt * 2]" />
    <TresMeshStandardMaterial color="#ffffff" :roughness="0.5" />
  </TresMesh>
  <!-- Right baseboard -->
  <TresMesh :position="[px + w / 2 - wt - bt / 2, bh / 2, pz]">
    <TresBoxGeometry :args="[bt, bh, d - wt * 2]" />
    <TresMeshStandardMaterial color="#ffffff" :roughness="0.5" />
  </TresMesh>

  <!-- ========== CROWN MOLDING (top of walls, subtle) ========== -->
  <!-- Back crown -->
  <TresMesh :position="[px, h - 0.02, pz - d / 2 + wt + 0.015]">
    <TresBoxGeometry :args="[w - wt * 2, 0.04, 0.03]" />
    <TresMeshStandardMaterial color="#ffffff" :roughness="0.4" />
  </TresMesh>
  <!-- Left crown -->
  <TresMesh :position="[px - w / 2 + wt + 0.015, h - 0.02, pz]">
    <TresBoxGeometry :args="[0.03, 0.04, d - wt * 2]" />
    <TresMeshStandardMaterial color="#ffffff" :roughness="0.4" />
  </TresMesh>

  <!-- ========== CEILING (partial, to cast shadow) ========== -->
  <TresMesh :position="[px, h, pz]" receive-shadow>
    <TresBoxGeometry :args="[w, 0.04, d]" />
    <TresMeshStandardMaterial color="#fafafa" :roughness="0.95" />
  </TresMesh>
</template>
