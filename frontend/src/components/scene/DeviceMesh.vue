<script setup lang="ts">
import { computed, watch, ref } from 'vue'
import gsap from 'gsap'
import type { DeviceState } from '@/types/world-state'

const props = defineProps<{
  device: DeviceState
}>()

// Room positions mapping
const roomPositions: Record<string, [number, number, number]> = {
  living_room: [-3.5, 2.5, -1],
  bedroom: [3.5, 2.5, -1],
  kitchen: [-3.5, 2.5, 2],
  bathroom: [3.5, 2.5, 2],
}

const position = computed<[number, number, number]>(() => {
  return roomPositions[props.device.location.room] ?? [0, 1, 0]
})

// --- Light-specific ---
const emissiveIntensity = ref(0)
const lightIntensity = ref(0)

const lightColor = computed(() => {
  const colorTemp = props.device.state.extra.color_temp ?? 4000
  if (colorTemp < 3500) return '#ff9f43'
  if (colorTemp < 4500) return '#fff5e6'
  return '#dfe6e9'
})

// Animate brightness changes with GSAP
watch(
  () => props.device.state.extra.brightness,
  (newBrightness: number) => {
    const targetIntensity = props.device.state.power ? (newBrightness ?? 0) / 100 : 0
    gsap.to(emissiveIntensity, {
      value: targetIntensity,
      duration: 0.8,
      ease: 'power2.out',
    })
    gsap.to(lightIntensity, {
      value: targetIntensity * 2,
      duration: 0.8,
      ease: 'power2.out',
    })
  },
  { immediate: true },
)

// --- HVAC-specific ---
const hvacColor = computed(() => {
  return props.device.state.power ? '#4a90d9' : '#555555'
})

// --- Curtain-specific ---
const curtainOpacity = computed(() => {
  const openPercent = props.device.state.extra.open_percent ?? 0
  return 1 - (openPercent / 100) * 0.8
})

const curtainColor = computed(() => {
  const openPercent = props.device.state.extra.open_percent ?? 0
  return openPercent > 50 ? '#4a4a5a' : '#2a2a3a'
})

const deviceType = computed(() => props.device.type)
</script>

<template>
  <!-- Light device -->
  <template v-if="deviceType === 'light'">
    <!-- Lamp body: cylinder -->
    <TresMesh :position="position">
      <TresCylinderGeometry :args="[0.15, 0.25, 0.6, 16]" />
      <TresMeshStandardMaterial
        :color="lightColor"
        :emissive="lightColor"
        :emissive-intensity="emissiveIntensity"
      />
    </TresMesh>
    <!-- Lamp glow: point light -->
    <TresPointLight
      :position="[position[0], position[1] + 0.5, position[2]]"
      :intensity="lightIntensity"
      :color="lightColor"
      :distance="6"
    />
  </template>

  <!-- HVAC device -->
  <template v-else-if="deviceType === 'hvac'">
    <!-- Main unit body -->
    <TresMesh :position="position">
      <TresBoxGeometry :args="[0.8, 0.3, 0.2]" />
      <TresMeshStandardMaterial :color="hvacColor" />
    </TresMesh>
    <!-- Vent grille -->
    <TresMesh :position="[position[0], position[1] - 0.2, position[2]]">
      <TresBoxGeometry :args="[0.6, 0.08, 0.05]" />
      <TresMeshStandardMaterial color="#333344" />
    </TresMesh>
  </template>

  <!-- Curtain device -->
  <template v-else-if="deviceType === 'curtain'">
    <TresMesh :position="[position[0], position[1] + 0.5, position[2] - 0.9]">
      <TresBoxGeometry :args="[1.2, 2.0, 0.05]" />
      <TresMeshStandardMaterial
        :color="curtainColor"
        :opacity="curtainOpacity"
        :transparent="true"
      />
    </TresMesh>
  </template>
</template>
