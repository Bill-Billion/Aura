import { shallowRef } from 'vue'
import * as THREE from 'three'
import type { CSS2DRenderer } from 'three/addons/renderers/CSS2DRenderer.js'

export const showroomRuntime = {
  environment: shallowRef<THREE.Texture | null>(null),
  labelRenderer: shallowRef<CSS2DRenderer | null>(null),
  camera: shallowRef<THREE.PerspectiveCamera | null>(null),
  scene: shallowRef<THREE.Scene | null>(null),
  onFrame: shallowRef<((dt: number, elapsed: number) => void) | null>(null),
}
