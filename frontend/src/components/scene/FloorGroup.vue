<script setup lang="ts">
import { useGLTF } from '@tresjs/cientos'
import * as THREE from 'three'
import type { FloorConfig } from '@/types/model-types'

const props = defineProps<{
  config: FloorConfig
}>()

const gltf = await useGLTF(props.config.modelPath)
const sourceScene = gltf.state.value?.scene ?? new THREE.Group()
const cloned = sourceScene.clone(true)
cloned.position.set(0, props.config.collapsedY, 0)

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
