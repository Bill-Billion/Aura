import * as THREE from 'three'

export interface FloorLightConfig {
  floorId: string
  numLights: number
  positions: Array<[number, number, number]>
  floorY: number
}

interface LightState {
  uniform: THREE.Vector4  // xyz=worldPos, w=currentIntensity
  target: number           // target intensity (0 or 1)
  localPosition: THREE.Vector3
}

const floorLights = new Map<string, LightState[]>()

export function useLightUniforms() {
  function setFloorTransform(floorId: string, position: THREE.Vector3Like) {
    const states = floorLights.get(floorId)
    if (!states) return

    for (const light of states) {
      light.uniform.x = light.localPosition.x + position.x
      light.uniform.y = light.localPosition.y + position.y
      light.uniform.z = light.localPosition.z + position.z
    }
  }

  function initFloor(config: FloorLightConfig): THREE.Vector4[] {
    const states: LightState[] = []
    const uniforms: THREE.Vector4[] = []

    for (let i = 0; i < config.numLights; i++) {
      const pos = config.positions[i] ?? [0, 0, 0]
      const localPosition = new THREE.Vector3(pos[0], pos[1], pos[2])
      const vec = new THREE.Vector4(
        pos[0], pos[1] + config.floorY, pos[2],
        1.0 // start with lights on
      )
      states.push({ uniform: vec, target: 1.0, localPosition })
      uniforms.push(vec)
    }

    floorLights.set(config.floorId, states)
    setFloorTransform(config.floorId, new THREE.Vector3(0, config.floorY, 0))
    return uniforms
  }

  function setLightTarget(floorId: string, lightIndex: number, intensity: number) {
    const states = floorLights.get(floorId)
    if (states && states[lightIndex]) {
      states[lightIndex].target = intensity
    }
  }

  /** Call every frame to lerp light intensities */
  function update(dt: number) {
    for (const [, states] of floorLights) {
      for (const light of states) {
        const current = light.uniform.w
        const next = THREE.MathUtils.lerp(current, light.target, Math.min(5 * dt, 1))
        light.uniform.w = next
      }
    }
  }

  function getFloorUniforms(floorId: string): THREE.Vector4[] {
    const states = floorLights.get(floorId)
    return states ? states.map(s => s.uniform) : []
  }

  return {
    initFloor,
    setLightTarget,
    setFloorTransform,
    update,
    getFloorUniforms,
  }
}
