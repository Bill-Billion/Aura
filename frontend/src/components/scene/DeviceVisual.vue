<script setup lang="ts">
import { computed, watch, ref, onBeforeUnmount } from 'vue'
import gsap from 'gsap'
import { useWorldStore } from '@/stores/worldStore'
import { useUIStore } from '@/stores/uiStore'
import type { Vec3 } from '@/types/scene-config'

const props = defineProps<{
  deviceId: string
  anchor: Vec3
  deviceType: 'light' | 'hvac' | 'curtain'
}>()

const worldStore = useWorldStore()
const uiStore = useUIStore()
const device = computed(() => worldStore.devices[props.deviceId])

// --- Pulse effect on state change ---
const pulseScale = ref(1)

function triggerPulse() {
  gsap.fromTo(pulseScale, { value: 1.05 }, { value: 1.0, duration: 0.3, ease: 'power2.out' })
}

// --- Hover state ---
const hovered = ref(false)
const hoverEmissive = ref(0)

function onPointerEnter() {
  hovered.value = true
  gsap.to(hoverEmissive, { value: 0.3, duration: 0.2 })
}

function onPointerLeave() {
  hovered.value = false
  gsap.to(hoverEmissive, { value: 0, duration: 0.2 })
}

function onClick() {
  uiStore.setActiveDevice(props.deviceId)
}

// --- Light ---
const emissiveIntensity = ref(0)
const lightIntensity = ref(0)

const lightColor = computed(() => {
  if (!device.value) return '#ffffff'
  const colorTemp = device.value.state.extra.color_temp ?? 4000
  if (colorTemp < 3500) return '#ff9f43'
  if (colorTemp < 4500) return '#fff5e6'
  return '#dfe6e9'
})

watch(
  () => device.value?.state.extra.brightness,
  (newBrightness: number | undefined) => {
    const brightness = newBrightness ?? 0
    const targetIntensity = device.value?.state.power ? brightness / 100 : 0
    gsap.to(emissiveIntensity, { value: targetIntensity, duration: 0.8, ease: 'power2.out' })
    gsap.to(lightIntensity, { value: targetIntensity * 2, duration: 0.8, ease: 'power2.out' })
  },
  { immediate: true },
)

// Also watch power state
watch(
  () => device.value?.state.power,
  (power) => {
    if (!power) {
      gsap.to(emissiveIntensity, { value: 0, duration: 0.5, ease: 'power2.out' })
      gsap.to(lightIntensity, { value: 0, duration: 0.5, ease: 'power2.out' })
    }
    triggerPulse()
  },
)

// --- HVAC ---
const hvacColor = computed(() => {
  if (!device.value?.state.power) return '#9E9E9E'
  const mode = device.value.state.extra.mode
  if (mode === 'cool') return '#4FC3F7'
  if (mode === 'heat') return '#EF5350'
  return '#4FC3F7'
})

// --- Curtain ---
const curtainScaleX = ref(1)
const curtainOpacity = ref(1)

watch(
  () => device.value?.state.extra.open_percent,
  (openPercent: number | undefined) => {
    const open = openPercent ?? 0
    gsap.to(curtainScaleX, { value: 1 - (open / 100) * 0.8, duration: 0.6, ease: 'power2.out' })
    gsap.to(curtainOpacity, { value: 1 - (open / 100) * 0.6, duration: 0.6, ease: 'power2.out' })
  },
  { immediate: true },
)

const curtainColor = computed(() => {
  const open = device.value?.state.extra.open_percent ?? 0
  return open > 50 ? '#4a4a5a' : '#2a2a3a'
})

// Cleanup GSAP tweens on unmount
onBeforeUnmount(() => {
  gsap.killTweensOf(emissiveIntensity)
  gsap.killTweensOf(lightIntensity)
  gsap.killTweensOf(curtainScaleX)
  gsap.killTweensOf(curtainOpacity)
  gsap.killTweensOf(hoverEmissive)
  gsap.killTweensOf(pulseScale)
})
</script>

<template>
  <template v-if="deviceType === 'light'">
    <TresMesh
      :position="anchor"
      :name="deviceId"
      :scale="pulseScale"
      @pointer-enter="onPointerEnter"
      @pointer-leave="onPointerLeave"
      @click="onClick"
    >
      <TresSphereGeometry :args="[0.12, 16, 16]" />
      <TresMeshStandardMaterial
        :color="lightColor"
        :emissive="lightColor"
        :emissive-intensity="emissiveIntensity + hoverEmissive"
      />
    </TresMesh>
    <TresPointLight
      :position="[anchor[0], anchor[1] + 0.3, anchor[2]]"
      :intensity="lightIntensity"
      :color="lightColor"
      :distance="8"
    />
  </template>

  <template v-else-if="deviceType === 'hvac'">
    <TresMesh
      :position="anchor"
      :name="deviceId"
      :scale="pulseScale"
      @pointer-enter="onPointerEnter"
      @pointer-leave="onPointerLeave"
      @click="onClick"
    >
      <TresBoxGeometry :args="[0.8, 0.25, 0.18]" />
      <TresMeshStandardMaterial
        :color="hvacColor"
        :emissive="hvacColor"
        :emissive-intensity="(device?.state.power ? 0.3 : 0) + hoverEmissive"
      />
    </TresMesh>
    <TresMesh :position="[anchor[0], anchor[1] - 0.18, anchor[2]]">
      <TresBoxGeometry :args="[0.6, 0.06, 0.04]" />
      <TresMeshStandardMaterial color="#333344" />
    </TresMesh>
  </template>

  <template v-else-if="deviceType === 'curtain'">
    <TresMesh
      :position="anchor"
      :name="deviceId"
      :scale="pulseScale"
      @pointer-enter="onPointerEnter"
      @pointer-leave="onPointerLeave"
      @click="onClick"
    >
      <TresBoxGeometry :args="[1.2 * curtainScaleX, 2.0, 0.04]" />
      <TresMeshStandardMaterial
        :color="curtainColor"
        :opacity="curtainOpacity"
        :transparent="true"
        :emissive="curtainColor"
        :emissive-intensity="hoverEmissive"
      />
    </TresMesh>
  </template>
</template>
