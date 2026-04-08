<script setup lang="ts">
import { ref, watch, shallowRef } from 'vue'
import { useGLTF } from '@tresjs/cientos'
import * as THREE from 'three'
import type { FloorConfig } from '@/types/model-types'

const props = defineProps<{
  config: FloorConfig
}>()

const groupRef = ref<THREE.Group | null>(null)

// useGLTF is async - Suspense will handle it
const { scene } = await useGLTF(props.config.modelPath)

// Clone and position
const cloned = scene.clone(true)
cloned.position.set(0, props.config.collapsedY, 0)

// Enable shadows
cloned.traverse((obj: THREE.Object3D) => {
  if ((obj as THREE.Mesh).isMesh) {
    obj.castShadow = true
    obj.receiveShadow = true
  }
})

defineExpose({ group: cloned })
</script>

<template>
  <primitive :object="cloned" />
</template>
