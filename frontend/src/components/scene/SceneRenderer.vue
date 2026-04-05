<script setup lang="ts">
import { TresCanvas } from '@tresjs/core'
import SceneManager from './SceneManager.vue'
</script>

<template>
  <div class="w-full h-full absolute inset-0">
    <TresCanvas clear-color="#0a0a12">
      <TresPerspectiveCamera :position="[12, 12, 12]" :fov="50" :look-at="[0, 0, 2]" />
      <TresAmbientLight :intensity="0.6" />
      <TresDirectionalLight :position="[8, 15, 10]" :intensity="1.2" />
      <TresDirectionalLight :position="[-5, 8, -8]" :intensity="0.3" color="#8888ff" />

      <Suspense>
        <SceneManager />
      </Suspense>
    </TresCanvas>
  </div>
</template>
