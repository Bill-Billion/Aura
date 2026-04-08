<script setup lang="ts">
import { ref, onMounted, onBeforeUnmount, inject, watch } from 'vue'
import { useLoop } from '@tresjs/core'
import { useSphericalCamera } from '@/composables/useSphericalCamera'
import { useCameraPresets } from '@/composables/useCameraPresets'
import { useUIStore } from '@/stores/uiStore'
import type { GamemcuSceneConfig } from '@/types/model-types'
import * as THREE from 'three'

const cameraRef = ref<THREE.PerspectiveCamera | null>(null)
const uiStore = useUIStore()
const { update, onPointerDown, onPointerMove, onPointerUp, onWheel, animateTo, setTarget } = useSphericalCamera()

// Get scene config from FloorManager
const sceneConfig = inject<{ value: GamemcuSceneConfig | null }>('gamemcuSceneConfig')

// Camera presets
let presets: ReturnType<typeof useCameraPresets> | null = null

watch(
  () => sceneConfig?.value,
  (config) => {
    if (config) {
      presets = useCameraPresets(config)
      setTarget(presets.overview)
    }
  },
  { immediate: true },
)

// Watch floor changes
watch(
  () => uiStore.activeFloor,
  (floorId) => {
    if (!presets) return
    if (floorId === 'overview') {
      animateTo(presets.overview, 0.8)
    } else {
      animateTo(presets.floorPreset(floorId), 0.8)
    }
  },
)

// Render loop
const { onBeforeRender } = useLoop()
onBeforeRender(({ delta }) => {
  const cam = cameraRef.value
  if (cam) {
    update(cam, delta)
  }
})

// Pointer events on canvas
let canvasEl: HTMLCanvasElement | null = null

onMounted(() => {
  // Find canvas element (TresCanvas renders a canvas)
  canvasEl = document.querySelector('canvas')
  if (canvasEl) {
    canvasEl.addEventListener('pointerdown', onPointerDown)
    canvasEl.addEventListener('pointermove', onPointerMove)
    canvasEl.addEventListener('pointerup', onPointerUp)
    canvasEl.addEventListener('pointerleave', onPointerUp)
    canvasEl.addEventListener('wheel', onWheel, { passive: true })
  }
})

onBeforeUnmount(() => {
  if (canvasEl) {
    canvasEl.removeEventListener('pointerdown', onPointerDown)
    canvasEl.removeEventListener('pointermove', onPointerMove)
    canvasEl.removeEventListener('pointerup', onPointerUp)
    canvasEl.removeEventListener('pointerleave', onPointerUp)
    canvasEl.removeEventListener('wheel', onWheel)
  }
})
</script>

<template>
  <TresPerspectiveCamera
    ref="cameraRef"
    :fov="12"
    :near="1"
    :far="300"
    :position="[-50, 60, 80]"
  />
</template>
