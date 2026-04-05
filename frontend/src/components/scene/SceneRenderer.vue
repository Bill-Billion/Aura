<script setup lang="ts">
import { TresCanvas } from '@tresjs/core'
import SceneManager from './SceneManager.vue'
</script>

<template>
  <div class="w-full h-full absolute inset-0">
    <TresCanvas clear-color="#1a1a2e" shadows>
      <TresPerspectiveCamera :position="[0, 12, 12]" :fov="50" :look-at="[0, 0, 3]" />
      <TresAmbientLight :intensity="0.8" color="#f8f6f0" />
      <TresDirectionalLight :position="[10, 15, 8]" :intensity="1.0" color="#fff8f0" cast-shadow />
      <TresDirectionalLight :position="[-8, 10, -6]" :intensity="0.3" color="#c8d0e0" />
      <TresHemisphereLight :args="['#f0e8d8', '#3a3a4e', 0.4]" />

      <Suspense>
        <SceneManager />
      </Suspense>
    </TresCanvas>
  </div>
</template>
