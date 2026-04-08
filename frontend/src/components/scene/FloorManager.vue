<script setup lang="ts">
import { ref, onMounted, provide } from 'vue'
import FloorGroup from './FloorGroup.vue'
import type { FloorConfig, GamemcuSceneConfig } from '@/types/model-types'

const sceneConfig = ref<GamemcuSceneConfig | null>(null)
const floorRefs = ref<Record<string, InstanceType<typeof FloorGroup>>>({})
const loaded = ref(false)

async function loadSceneConfig() {
  try {
    const resp = await fetch('/scenes/gamemcu_home.json')
    sceneConfig.value = await resp.json()
    loaded.value = true
  } catch (e) {
    console.error('Failed to load gamemcu scene config:', e)
  }
}

function setFloorRef(floorId: string, el: any) {
  if (el) {
    floorRefs.value[floorId] = el
  }
}

onMounted(loadSceneConfig)

// Provide scene config to children
provide('gamemcuSceneConfig', sceneConfig)
</script>

<template>
  <TresGroup v-if="sceneConfig">
    <FloorGroup
      v-for="floor in sceneConfig.floors"
      :key="floor.id"
      :config="floor"
      :ref="(el: any) => setFloorRef(floor.id, el)"
    />
  </TresGroup>
</template>
