<script setup lang="ts">
import type { Vec3 } from '@/types/scene-config'

const props = defineProps<{
  position: Vec3
}>()

const px = props.position[0]
const pz = props.position[2]
</script>

<template>
  <!-- ===================== WALK-IN SHOWER (glass partition) ===================== -->
  <!-- Shower tray (slate-like) -->
  <TresMesh :position="[px - 0.9, 0.025, pz - 1.35]" receive-shadow>
    <TresBoxGeometry :args="[1.4, 0.04, 1.2]" />
    <TresMeshStandardMaterial color="#9a9590" :roughness="0.7" :metalness="0.05" />
  </TresMesh>
  <!-- Drain -->
  <TresMesh :position="[px - 0.9, 0.046, pz - 1.35]" :rotation-x="-Math.PI / 2">
    <TresRingGeometry :args="[0.03, 0.06, 16]" />
    <TresMeshStandardMaterial color="#888" :roughness="0.2" :metalness="0.5" />
  </TresMesh>

  <!-- Glass wall (frameless, modern) -->
  <TresMesh :position="[px - 0.2, 1.15, pz - 1.35]">
    <TresBoxGeometry :args="[0.015, 2.1, 1.2]" />
    <TresMeshStandardMaterial color="#d8e8f0" :roughness="0.02" :metalness="0.0" :transparent="true" :opacity="0.15" />
  </TresMesh>
  <!-- Glass top edge (chrome strip) -->
  <TresMesh :position="[px - 0.2, 2.2, pz - 1.35]">
    <TresBoxGeometry :args="[0.02, 0.015, 1.2]" />
    <TresMeshStandardMaterial color="#d0d0d0" :roughness="0.1" :metalness="0.8" />
  </TresMesh>

  <!-- Shower head (rain style) -->
  <TresMesh :position="[px - 0.9, 2.4, pz - 1.9]">
    <TresBoxGeometry :args="[0.25, 0.015, 0.25]" />
    <TresMeshStandardMaterial color="#d0d0d0" :roughness="0.1" :metalness="0.8" />
  </TresMesh>
  <!-- Shower arm -->
  <TresMesh :position="[px - 0.9, 2.5, pz - 1.92]">
    <TresCylinderGeometry :args="[0.012, 0.012, 0.2, 8]" />
    <TresMeshStandardMaterial color="#d0d0d0" :roughness="0.1" :metalness="0.8" />
  </TresMesh>
  <!-- Hand shower bar -->
  <TresMesh :position="[px - 1.45, 1.3, pz - 1.9]">
    <TresCylinderGeometry :args="[0.01, 0.01, 0.9, 6]" />
    <TresMeshStandardMaterial color="#d0d0d0" :roughness="0.1" :metalness="0.8" />
  </TresMesh>

  <!-- ===================== VANITY (floating, modern) ===================== -->
  <!-- Vanity cabinet -->
  <TresMesh :position="[px + 1.0, 0.48, pz - 0.3]" cast-shadow>
    <TresBoxGeometry :args="[1.0, 0.35, 0.48]" />
    <TresMeshStandardMaterial color="#c4a67a" :roughness="0.5" />
  </TresMesh>
  <!-- Vanity countertop (white stone) -->
  <TresMesh :position="[px + 1.0, 0.67, pz - 0.3]">
    <TresBoxGeometry :args="[1.06, 0.025, 0.52]" />
    <TresMeshStandardMaterial color="#f0ece8" :roughness="0.2" :metalness="0.05" />
  </TresMesh>
  <!-- Sink basin (integrated) -->
  <TresMesh :position="[px + 1.0, 0.66, pz - 0.3]">
    <TresCylinderGeometry :args="[0.18, 0.16, 0.04, 24]" />
    <TresMeshStandardMaterial color="#e8e4e0" :roughness="0.15" :metalness="0.05" />
  </TresMesh>
  <!-- Faucet -->
  <TresMesh :position="[px + 1.0, 0.82, pz - 0.48]">
    <TresCylinderGeometry :args="[0.012, 0.012, 0.28, 8]" />
    <TresMeshStandardMaterial color="#d0d0d0" :roughness="0.1" :metalness="0.8" />
  </TresMesh>
  <TresMesh :position="[px + 1.0, 0.92, pz - 0.38]" :rotation-x="1.3">
    <TresCylinderGeometry :args="[0.008, 0.008, 0.16, 6]" />
    <TresMeshStandardMaterial color="#d0d0d0" :roughness="0.1" :metalness="0.8" />
  </TresMesh>
  <!-- Handle -->
  <TresMesh :position="[px + 1.0, 0.44, pz - 0.05]">
    <TresBoxGeometry :args="[0.2, 0.008, 0.005]" />
    <TresMeshStandardMaterial color="#c8a86e" :roughness="0.3" :metalness="0.7" />
  </TresMesh>

  <!-- ===================== MIRROR (large, LED backlit frame) ===================== -->
  <!-- Mirror surface -->
  <TresMesh :position="[px + 1.0, 1.4, pz - 0.53]">
    <TresBoxGeometry :args="[0.8, 0.9, 0.02]" />
    <TresMeshStandardMaterial color="#b8c8d8" :roughness="0.02" :metalness="0.9" />
  </TresMesh>
  <!-- Mirror frame (thin, dark) -->
  <TresMesh :position="[px + 1.0, 1.4, pz - 0.54]">
    <TresBoxGeometry :args="[0.84, 0.94, 0.015]" />
    <TresMeshStandardMaterial color="#2a2a2a" :roughness="0.4" />
  </TresMesh>

  <!-- ===================== TOILET (wall-hung, modern) ===================== -->
  <!-- Bowl -->
  <TresMesh :position="[px - 1.2, 0.24, pz + 1.0]" cast-shadow>
    <TresBoxGeometry :args="[0.38, 0.3, 0.55]" />
    <TresMeshStandardMaterial color="#f5f2f0" :roughness="0.3" :metalness="0.0" />
  </TresMesh>
  <!-- Seat -->
  <TresMesh :position="[px - 1.2, 0.4, pz + 0.88]">
    <TresBoxGeometry :args="[0.36, 0.02, 0.45]" />
    <TresMeshStandardMaterial color="#f0ece8" :roughness="0.35" />
  </TresMesh>
  <!-- Lid -->
  <TresMesh :position="[px - 1.2, 0.42, pz + 0.88]">
    <TresBoxGeometry :args="[0.34, 0.015, 0.43]" />
    <TresMeshStandardMaterial color="#f5f2f0" :roughness="0.3" />
  </TresMesh>
  <!-- Flush plate (on wall) -->
  <TresMesh :position="[px - 1.56, 0.8, pz + 1.2]">
    <TresBoxGeometry :args="[0.01, 0.15, 0.22]" />
    <TresMeshStandardMaterial color="#e0dcd8" :roughness="0.2" :metalness="0.3" />
  </TresMesh>
  <!-- Flush buttons -->
  <TresMesh :position="[px - 1.55, 0.82, pz + 1.15]">
    <TresBoxGeometry :args="[0.005, 0.06, 0.06]" />
    <TresMeshStandardMaterial color="#d0d0d0" :roughness="0.15" :metalness="0.5" />
  </TresMesh>
  <TresMesh :position="[px - 1.55, 0.82, pz + 1.25]">
    <TresBoxGeometry :args="[0.005, 0.06, 0.06]" />
    <TresMeshStandardMaterial color="#d0d0d0" :roughness="0.15" :metalness="0.5" />
  </TresMesh>

  <!-- ===================== TOWEL RACK (wall-mounted, chrome) ===================== -->
  <TresMesh :position="[px + 0.2, 1.2, pz - 1.9]">
    <TresCylinderGeometry :args="[0.01, 0.01, 0.6, 6]" :rotation-z="Math.PI / 2" />
    <TresMeshStandardMaterial color="#d0d0d0" :roughness="0.1" :metalness="0.8" />
  </TresMesh>
  <!-- Towel (draped) -->
  <TresMesh :position="[px + 0.2, 0.95, pz - 1.88]">
    <TresBoxGeometry :args="[0.5, 0.4, 0.04]" />
    <TresMeshStandardMaterial color="#f5f0e8" :roughness="0.9" />
  </TresMesh>

  <!-- ===================== BATH MAT ===================== -->
  <TresMesh :position="[px + 0.3, 0.03, pz + 0.5]" :rotation-x="-Math.PI / 2">
    <TresPlaneGeometry :args="[0.65, 0.45]" />
    <TresMeshStandardMaterial color="#a0b4a8" :roughness="0.98" />
  </TresMesh>

  <!-- ===================== SHELF (floating, back wall) ===================== -->
  <TresMesh :position="[px + 0.2, 1.6, pz - 1.92]">
    <TresBoxGeometry :args="[0.6, 0.025, 0.15]" />
    <TresMeshStandardMaterial color="#c4a67a" :roughness="0.5" />
  </TresMesh>
  <!-- Items on shelf -->
  <TresMesh :position="[px + 0.05, 1.65, pz - 1.92]">
    <TresCylinderGeometry :args="[0.03, 0.03, 0.08, 8]" />
    <TresMeshStandardMaterial color="#f5f0e8" :roughness="0.6" />
  </TresMesh>
  <TresMesh :position="[px + 0.2, 1.65, pz - 1.92]">
    <TresCylinderGeometry :args="[0.025, 0.025, 0.1, 8]" />
    <TresMeshStandardMaterial color="#8ab4a0" :roughness="0.6" />
  </TresMesh>
  <TresMesh :position="[px + 0.38, 1.66, pz - 1.92]">
    <TresSphereGeometry :args="[0.04, 8, 8]" />
    <TresMeshStandardMaterial color="#3a6e4a" :roughness="0.85" />
  </TresMesh>
</template>
