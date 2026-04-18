import { defineComponent, toRaw } from 'vue'
import * as THREE from 'three'
import { useLoop, useTres } from '@tresjs/core'
import { useSphericalCamera } from '@/composables/useSphericalCamera'
import { useLightUniforms } from '@/composables/useLightUniforms'
import { updateDeviceAnimations } from '@/composables/useDeviceAnimations'
import { showroomRuntime } from './showroomRuntime'

export const SceneRenderLoop = defineComponent({
  name: 'SceneRenderLoop',
  setup() {
    const cam = useSphericalCamera()
    const lights = useLightUniforms()
    const { camera, scene } = useTres()
    const { onBeforeRender } = useLoop()

    let elapsed = 0

    onBeforeRender(({ delta }) => {
      elapsed += delta

      const activeScene = scene.value ? toRaw(scene.value) : null
      const activeCamera = camera.value ? (toRaw(camera.value) as THREE.PerspectiveCamera) : null
      if (!activeScene || !activeCamera) return

      showroomRuntime.scene.value = activeScene
      showroomRuntime.camera.value = activeCamera
      activeScene.background = new THREE.Color(0x090c12)
      if (showroomRuntime.environment.value) {
        activeScene.environment = showroomRuntime.environment.value
      }

      cam.update(activeCamera, delta)
      lights.update(delta)
      updateDeviceAnimations(delta)
      showroomRuntime.onFrame.value?.(delta, elapsed)
      showroomRuntime.labelRenderer.value?.render(activeScene, activeCamera)
    })

    return () => null
  },
})
