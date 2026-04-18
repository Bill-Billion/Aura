import { reactive } from 'vue'
import * as THREE from 'three'
import type { CameraPreset } from '@/types/model-types'
import { showroomVisualConfig } from '@/config/showroomVisualConfig'
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
  isDragging: boolean
  lastPointerX: number
  lastPointerY: number
  phiMin: number
  phiMax: number
  distanceMin: number
  distanceMax: number
}

const overviewPreset = showroomVisualConfig.camera.overview

const state = reactive<SphericalCameraState>({
  targetTheta: overviewPreset.theta,
  targetPhi: overviewPreset.phi,
  targetSpringLength: overviewPreset.springLength,
  targetLookAt: new THREE.Vector3(...overviewPreset.lookAt),
  targetFov: overviewPreset.fov ?? 12,
  currentTheta: overviewPreset.theta,
  currentPhi: overviewPreset.phi,
  currentSpringLength: overviewPreset.springLength,
  currentLookAt: new THREE.Vector3(...overviewPreset.lookAt),
  currentFov: overviewPreset.fov ?? 12,
  smoothing: overviewPreset.smoothing ?? 4,
  rotateSmoothing: overviewPreset.rotateSmoothing ?? 4,
  isDragging: false,
  lastPointerX: 0,
  lastPointerY: 0,
  phiMin: 0.84,
  phiMax: 1.26,
  distanceMin: 32,
  distanceMax: 150,
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
    const posFactor = 1 - Math.exp(-state.smoothing * dt)
    const rotFactor = 1 - Math.exp(-state.rotateSmoothing * dt)

    state.currentTheta += (state.targetTheta - state.currentTheta) * rotFactor
    state.currentPhi += (state.targetPhi - state.currentPhi) * rotFactor
    state.currentSpringLength += (state.targetSpringLength - state.currentSpringLength) * posFactor
    state.currentLookAt.lerp(state.targetLookAt, posFactor)
    state.currentFov += (state.targetFov - state.currentFov) * posFactor

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

  function onPointerDown(event: PointerEvent) {
    state.isDragging = true
    state.lastPointerX = event.clientX
    state.lastPointerY = event.clientY
  }

  function onPointerMove(event: PointerEvent) {
    if (!state.isDragging) return
    const dx = event.clientX - state.lastPointerX
    state.lastPointerX = event.clientX
    state.lastPointerY = event.clientY
    state.targetTheta -= dx * 0.0052
  }

  function onPointerUp() {
    state.isDragging = false
  }

  function onWheel(event: WheelEvent) {
    const delta = event.deltaY * 0.08
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
