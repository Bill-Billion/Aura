<script setup lang="ts">
import { watch, ref } from 'vue'
import { useLoop } from '@tresjs/core'
import { useUIStore } from '@/stores/uiStore'
import { useSceneConfig } from '@/composables/useSceneConfig'
import gsap from 'gsap'
import * as THREE from 'three'

const uiStore = useUIStore()
const { config } = useSceneConfig()

const cameraRef = ref<THREE.PerspectiveCamera | null>(null)

const currentLookAt = new THREE.Vector3(0, 0, 3)
let isAnimating = false

function getPreset(roomId: string) {
  if (!config.value) return null
  const room = config.value.rooms.find(r => r.id === roomId)
  if (!room) return null
  const [rx, , rz] = room.position
  return {
    position: new THREE.Vector3(rx + 4, 7, rz + 7),
    lookAt: new THREE.Vector3(rx, 0.5, rz),
  }
}

watch(() => uiStore.activeRoom, (roomId) => {
  const preset = getPreset(roomId)
  const cam = cameraRef.value
  if (!preset || !cam) return

  isAnimating = true

  gsap.to(cam.position, {
    x: preset.position.x,
    y: preset.position.y,
    z: preset.position.z,
    duration: 1.2,
    ease: 'power2.inOut',
    onComplete: () => { isAnimating = false },
  })

  gsap.to(currentLookAt, {
    x: preset.lookAt.x,
    y: preset.lookAt.y,
    z: preset.lookAt.z,
    duration: 1.2,
    ease: 'power2.inOut',
  })
})

const { onBeforeRender } = useLoop()
onBeforeRender(() => {
  const cam = cameraRef.value
  if (cam && isAnimating) {
    cam.lookAt(currentLookAt)
  }
})
</script>

<template>
  <TresPerspectiveCamera
    ref="cameraRef"
    :position="[0, 12, 12]"
    :fov="50"
    :look-at="[0, 0, 3]"
  />
</template>
