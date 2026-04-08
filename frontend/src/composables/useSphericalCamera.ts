import { reactive } from 'vue'
import * as THREE from 'three'
import type { CameraPreset } from '@/types/model-types'
import gsap from 'gsap'

export interface SphericalCameraState {
  targetTheta: number
  targetPhi: number
  targetSpringLength: number
  targetLookAt: THREE.Vector3
  targetFov: number

  currentTheta: number
  currentPhi: number
  currentSpringLength: number
  currentLookAt: THREE.Vector3
  currentFov: number

  smoothing: number
  rotateSmoothing: number

  // Input state
  isDragging: boolean
  lastPointerX: number
  lastPointerY: number

  // Limits
  phiMin: number
  phiMax: number
  distanceMin: number
  distanceMax: number
}

const state = reactive<SphericalCameraState>({
  // gamemcu overview defaults — theta rotated 180° (+ PI) to match gamemcu front view
  targetTheta: 2.38 + Math.PI,
  targetPhi: 1.25,
  targetSpringLength: 120,
  targetLookAt: new THREE.Vector3(-1.5, 7.5, 0.5),
  targetFov: 12,

  currentTheta: 2.38 + Math.PI,
  currentPhi: 1.25,
  currentSpringLength: 120,
  currentLookAt: new THREE.Vector3(-1.5, 7.5, 0.5),
  currentFov: 12,

  smoothing: 4,
  rotateSmoothing: 4,

  isDragging: false,
  lastPointerX: 0,
  lastPointerY: 0,

  phiMin: 0.2,
  phiMax: 1.4,
  distanceMin: 20,
  distanceMax: 180,
})

export function useSphericalCamera() {

  function setTarget(preset: CameraPreset) {
    state.targetTheta = preset.theta
    state.targetPhi = preset.phi
    state.targetSpringLength = preset.springLength
    state.targetLookAt.set(...preset.lookAt)
    if (preset.fov) state.targetFov = preset.fov
    if (preset.smoothing) state.smoothing = preset.smoothing
    if (preset.rotateSmoothing) state.rotateSmoothing = preset.rotateSmoothing
  }

  function animateTo(preset: CameraPreset, duration = 0.8) {
    gsap.to(state, {
      targetTheta: preset.theta,
      targetPhi: preset.phi,
      targetSpringLength: preset.springLength,
      duration,
      ease: 'cubic.out',
    })
    gsap.to(state.targetLookAt, {
      x: preset.lookAt[0],
      y: preset.lookAt[1],
      z: preset.lookAt[2],
      duration,
      ease: 'cubic.out',
    })
    if (preset.fov) {
      gsap.to(state, { targetFov: preset.fov, duration, ease: 'cubic.out' })
    }
    if (preset.smoothing) state.smoothing = preset.smoothing
    if (preset.rotateSmoothing) state.rotateSmoothing = preset.rotateSmoothing
  }

  function update(camera: THREE.PerspectiveCamera, dt: number) {
    // Spring damping interpolation
    const posFactor = 1 - Math.exp(-state.smoothing * dt)
    const rotFactor = 1 - Math.exp(-state.rotateSmoothing * dt)

    state.currentTheta += (state.targetTheta - state.currentTheta) * rotFactor
    state.currentPhi += (state.targetPhi - state.currentPhi) * rotFactor
    state.currentSpringLength += (state.targetSpringLength - state.currentSpringLength) * posFactor
    state.currentLookAt.lerp(state.targetLookAt, posFactor)
    state.currentFov += (state.targetFov - state.currentFov) * posFactor

    // Spherical to cartesian
    const r = state.currentSpringLength
    const phi = state.currentPhi
    const theta = state.currentTheta

    camera.position.set(
      state.currentLookAt.x + r * Math.sin(phi) * Math.cos(theta),
      state.currentLookAt.y + r * Math.cos(phi),
      state.currentLookAt.z + r * Math.sin(phi) * Math.sin(theta),
    )
    camera.lookAt(state.currentLookAt)
    camera.fov = state.currentFov
    camera.updateProjectionMatrix()
  }

  // --- Pointer handlers ---

  function onPointerDown(e: PointerEvent) {
    state.isDragging = true
    state.lastPointerX = e.clientX
    state.lastPointerY = e.clientY
  }

  function onPointerMove(e: PointerEvent) {
    if (!state.isDragging) return
    const dx = e.clientX - state.lastPointerX
    const dy = e.clientY - state.lastPointerY
    state.lastPointerX = e.clientX
    state.lastPointerY = e.clientY

    // Rotate — horizontal only (gamemcu locks vertical axis)
    state.targetTheta -= dx * 0.005
    // phi is locked — no vertical rotation allowed
  }

  function onPointerUp() {
    state.isDragging = false
  }

  function onWheel(e: WheelEvent) {
    const delta = e.deltaY * 0.1
    state.targetSpringLength = Math.max(
      state.distanceMin,
      Math.min(state.distanceMax, state.targetSpringLength + delta),
    )
  }

  return {
    state,
    setTarget,
    animateTo,
    update,
    onPointerDown,
    onPointerMove,
    onPointerUp,
    onWheel,
  }
}
