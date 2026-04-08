<script setup lang="ts">
import type { Vec3 } from '@/types/scene-config'

const props = defineProps<{
  position: Vec3
}>()

const px = props.position[0]
const pz = props.position[2]
</script>

<template>
  <!-- ===================== SOFA (modern L-shape, dark gray fabric) ===================== -->
  <!-- Sofa base/frame -->
  <TresMesh :position="[px - 0.2, 0.18, pz - 1.8]" cast-shadow>
    <TresBoxGeometry :args="[2.6, 0.12, 1.0]" />
    <TresMeshStandardMaterial color="#2a2a2a" :roughness="0.9" />
  </TresMesh>
  <!-- Sofa seat cushions -->
  <TresMesh :position="[px - 0.5, 0.32, pz - 1.75]" cast-shadow>
    <TresBoxGeometry :args="[1.0, 0.16, 0.88]" />
    <TresMeshStandardMaterial color="#404040" :roughness="0.92" />
  </TresMesh>
  <TresMesh :position="[px + 0.4, 0.32, pz - 1.75]" cast-shadow>
    <TresBoxGeometry :args="[0.9, 0.16, 0.88]" />
    <TresMeshStandardMaterial color="#3d3d3d" :roughness="0.92" />
  </TresMesh>
  <!-- Sofa back -->
  <TresMesh :position="[px - 0.2, 0.58, pz - 2.22]" cast-shadow>
    <TresBoxGeometry :args="[2.6, 0.46, 0.14]" />
    <TresMeshStandardMaterial color="#363636" :roughness="0.88" />
  </TresMesh>
  <!-- Sofa back cushions (softer) -->
  <TresMesh :position="[px - 0.5, 0.56, pz - 2.12]" cast-shadow>
    <TresBoxGeometry :args="[0.9, 0.36, 0.1]" />
    <TresMeshStandardMaterial color="#4a4a4a" :roughness="0.95" />
  </TresMesh>
  <TresMesh :position="[px + 0.4, 0.56, pz - 2.12]" cast-shadow>
    <TresBoxGeometry :args="[0.8, 0.36, 0.1]" />
    <TresMeshStandardMaterial color="#484848" :roughness="0.95" />
  </TresMesh>
  <!-- Sofa left arm -->
  <TresMesh :position="[px - 1.5, 0.42, pz - 1.8]" cast-shadow>
    <TresBoxGeometry :args="[0.12, 0.38, 0.96]" />
    <TresMeshStandardMaterial color="#363636" :roughness="0.88" />
  </TresMesh>
  <!-- Sofa right arm -->
  <TresMesh :position="[px + 1.1, 0.42, pz - 1.8]" cast-shadow>
    <TresBoxGeometry :args="[0.12, 0.38, 0.96]" />
    <TresMeshStandardMaterial color="#363636" :roughness="0.88" />
  </TresMesh>
  <!-- Sofa legs (metal, gold-ish accent) -->
  <TresMesh v-for="(lp, i) in [[-1.4, -2.2], [-1.4, -1.3], [1.0, -2.2], [1.0, -1.3]]" :key="'sleg-' + i"
    :position="[px + lp[0], 0.05, pz + lp[1]]">
    <TresCylinderGeometry :args="[0.02, 0.02, 0.1, 8]" />
    <TresMeshStandardMaterial color="#c8a86e" :roughness="0.3" :metalness="0.7" />
  </TresMesh>
  <!-- Throw pillows (accent color) -->
  <TresMesh :position="[px - 1.2, 0.48, pz - 1.9]" :rotation-z="0.15" cast-shadow>
    <TresBoxGeometry :args="[0.35, 0.35, 0.08]" />
    <TresMeshStandardMaterial color="#d4a04a" :roughness="0.9" />
  </TresMesh>
  <TresMesh :position="[px + 0.8, 0.48, pz - 1.85]" :rotation-z="-0.1" cast-shadow>
    <TresBoxGeometry :args="[0.32, 0.32, 0.08]" />
    <TresMeshStandardMaterial color="#6b8e8e" :roughness="0.9" />
  </TresMesh>

  <!-- ===================== COFFEE TABLE (walnut top, metal legs) ===================== -->
  <!-- Table top -->
  <TresMesh :position="[px - 0.2, 0.32, pz - 0.5]" cast-shadow>
    <TresBoxGeometry :args="[1.1, 0.035, 0.55]" />
    <TresMeshStandardMaterial color="#6b4e31" :roughness="0.45" :metalness="0.0" />
  </TresMesh>
  <!-- Lower shelf -->
  <TresMesh :position="[px - 0.2, 0.12, pz - 0.5]">
    <TresBoxGeometry :args="[0.95, 0.02, 0.42]" />
    <TresMeshStandardMaterial color="#7a5c3e" :roughness="0.5" />
  </TresMesh>
  <!-- Metal legs (hairpin style) -->
  <TresMesh v-for="(lp, i) in [[-0.45, -0.22], [-0.45, 0.22], [0.45, -0.22], [0.45, 0.22]]" :key="'ctleg-' + i"
    :position="[px - 0.2 + lp[0] * 1.1, 0.16, pz - 0.5 + lp[1] * 0.55]">
    <TresCylinderGeometry :args="[0.012, 0.012, 0.32, 6]" />
    <TresMeshStandardMaterial color="#1a1a1a" :roughness="0.35" :metalness="0.8" />
  </TresMesh>
  <!-- Decor on table: book stack -->
  <TresMesh :position="[px - 0.5, 0.37, pz - 0.5]" :rotation-y="0.2">
    <TresBoxGeometry :args="[0.2, 0.06, 0.15]" />
    <TresMeshStandardMaterial color="#2c4a3e" :roughness="0.85" />
  </TresMesh>
  <TresMesh :position="[px - 0.5, 0.40, pz - 0.48]" :rotation-y="-0.1">
    <TresBoxGeometry :args="[0.18, 0.025, 0.13]" />
    <TresMeshStandardMaterial color="#c8a872" :roughness="0.85" />
  </TresMesh>

  <!-- ===================== TV CONSOLE (floating, modern) ===================== -->
  <!-- Wall-mounted console -->
  <TresMesh :position="[px - 0.2, 0.35, pz + 2.15]" cast-shadow>
    <TresBoxGeometry :args="[2.4, 0.3, 0.42]" />
    <TresMeshStandardMaterial color="#f5f0e8" :roughness="0.6" />
  </TresMesh>
  <!-- Console dark drawer -->
  <TresMesh :position="[px - 0.2, 0.35, pz + 1.93]">
    <TresBoxGeometry :args="[2.36, 0.26, 0.01]" />
    <TresMeshStandardMaterial color="#d0c8bc" :roughness="0.5" />
  </TresMesh>
  <!-- Handle line -->
  <TresMesh :position="[px - 0.2, 0.35, pz + 1.92]">
    <TresBoxGeometry :args="[0.6, 0.008, 0.008]" />
    <TresMeshStandardMaterial color="#c8a86e" :roughness="0.3" :metalness="0.7" />
  </TresMesh>

  <!-- TV (thin-bezel, black) -->
  <TresMesh :position="[px - 0.2, 1.2, pz + 2.42]" cast-shadow>
    <TresBoxGeometry :args="[1.9, 1.08, 0.03]" />
    <TresMeshStandardMaterial color="#111111" :roughness="0.15" :metalness="0.1" />
  </TresMesh>
  <!-- TV screen -->
  <TresMesh :position="[px - 0.2, 1.2, pz + 2.405]">
    <TresBoxGeometry :args="[1.82, 1.02, 0.005]" />
    <TresMeshStandardMaterial color="#0a0a12" :roughness="0.05" :metalness="0.2" />
  </TresMesh>

  <!-- ===================== RUG (large, textured) ===================== -->
  <TresMesh :position="[px - 0.2, 0.03, pz - 1.0]" :rotation-x="-Math.PI / 2">
    <TresPlaneGeometry :args="[3.0, 2.2]" />
    <TresMeshStandardMaterial color="#b8a88a" :roughness="0.98" />
  </TresMesh>
  <!-- Rug border -->
  <TresMesh :position="[px - 0.2, 0.031, pz - 1.0]" :rotation-x="-Math.PI / 2">
    <TresPlaneGeometry :args="[3.1, 2.3]" />
    <TresMeshStandardMaterial color="#a09078" :roughness="0.98" />
  </TresMesh>

  <!-- ===================== FLOOR LAMP (modern arc) ===================== -->
  <!-- Base -->
  <TresMesh :position="[px + 1.6, 0.02, pz - 2.0]">
    <TresCylinderGeometry :args="[0.15, 0.15, 0.025, 16]" />
    <TresMeshStandardMaterial color="#1a1a1a" :roughness="0.35" :metalness="0.8" />
  </TresMesh>
  <!-- Pole -->
  <TresMesh :position="[px + 1.6, 0.85, pz - 2.0]">
    <TresCylinderGeometry :args="[0.015, 0.015, 1.65, 8]" />
    <TresMeshStandardMaterial color="#1a1a1a" :roughness="0.35" :metalness="0.8" />
  </TresMesh>
  <!-- Shade (warm) -->
  <TresMesh :position="[px + 1.6, 1.72, pz - 2.0]">
    <TresCylinderGeometry :args="[0.18, 0.12, 0.26, 16]" />
    <TresMeshStandardMaterial color="#f0e0c0" :roughness="0.8" />
  </TresMesh>

  <!-- ===================== SIDE TABLE ===================== -->
  <TresMesh :position="[px + 1.5, 0.28, pz - 1.2]" cast-shadow>
    <TresCylinderGeometry :args="[0.22, 0.22, 0.03, 24]" />
    <TresMeshStandardMaterial color="#f0e8d8" :roughness="0.6" :metalness="0.0" />
  </TresMesh>
  <TresMesh :position="[px + 1.5, 0.14, pz - 1.2]">
    <TresCylinderGeometry :args="[0.018, 0.018, 0.26, 8]" />
    <TresMeshStandardMaterial color="#c8a86e" :roughness="0.3" :metalness="0.7" />
  </TresMesh>
  <TresMesh :position="[px + 1.5, 0.005, pz - 1.2]">
    <TresCylinderGeometry :args="[0.12, 0.12, 0.01, 16]" />
    <TresMeshStandardMaterial color="#c8a86e" :roughness="0.3" :metalness="0.7" />
  </TresMesh>

  <!-- ===================== PLANT (decorative) ===================== -->
  <!-- Pot -->
  <TresMesh :position="[px - 2.0, 0.12, pz + 1.8]">
    <TresCylinderGeometry :args="[0.12, 0.1, 0.22, 12]" />
    <TresMeshStandardMaterial color="#f0e8d8" :roughness="0.7" />
  </TresMesh>
  <!-- Soil -->
  <TresMesh :position="[px - 2.0, 0.24, pz + 1.8]">
    <TresCylinderGeometry :args="[0.1, 0.1, 0.02, 12]" />
    <TresMeshStandardMaterial color="#4a3828" :roughness="0.95" />
  </TresMesh>
  <!-- Leaves (simplified spheres) -->
  <TresMesh :position="[px - 2.0, 0.45, pz + 1.8]">
    <TresSphereGeometry :args="[0.18, 8, 8]" />
    <TresMeshStandardMaterial color="#4a7a4a" :roughness="0.85" />
  </TresMesh>
  <TresMesh :position="[px - 1.92, 0.55, pz + 1.85]">
    <TresSphereGeometry :args="[0.12, 8, 8]" />
    <TresMeshStandardMaterial color="#5a8a50" :roughness="0.85" />
  </TresMesh>
  <TresMesh :position="[px - 2.08, 0.5, pz + 1.72]">
    <TresSphereGeometry :args="[0.1, 8, 8]" />
    <TresMeshStandardMaterial color="#3e6e3e" :roughness="0.85" />
  </TresMesh>

  <!-- ===================== WINDOW (panoramic, back wall) ===================== -->
  <!-- Window glass -->
  <TresMesh :position="[px + 0.6, 1.6, pz - 2.72]">
    <TresBoxGeometry :args="[1.8, 1.5, 0.02]" />
    <TresMeshStandardMaterial color="#c8dce8" :roughness="0.05" :metalness="0.1" :transparent="true" :opacity="0.2" />
  </TresMesh>
  <!-- Window frame -->
  <TresMesh :position="[px + 0.6, 1.6, pz - 2.71]">
    <TresBoxGeometry :args="[1.86, 1.56, 0.015]" />
    <TresMeshStandardMaterial color="#ffffff" :roughness="0.5" />
  </TresMesh>
  <!-- Window cross bars -->
  <TresMesh :position="[px + 0.6, 1.6, pz - 2.70]">
    <TresBoxGeometry :args="[0.015, 1.5, 0.01]" />
    <TresMeshStandardMaterial color="#ffffff" :roughness="0.5" />
  </TresMesh>
  <TresMesh :position="[px + 0.6, 1.6, pz - 2.70]">
    <TresBoxGeometry :args="[1.8, 0.015, 0.01]" />
    <TresMeshStandardMaterial color="#ffffff" :roughness="0.5" />
  </TresMesh>
</template>
