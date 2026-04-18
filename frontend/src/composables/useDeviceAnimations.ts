import * as THREE from 'three'
import { useWorldStore } from '@/stores/worldStore'
import { getFloorForDevice } from '@/utils/deviceFloorMap'

let cachedWorldStore: ReturnType<typeof useWorldStore> | null = null

const floorMaterials = new Map<string, THREE.ShaderMaterial[]>()
const hvacNodes = new Map<string, THREE.Object3D[]>()
const curtainNodes = new Map<string, THREE.Object3D[]>()
const lightCurrents = new Map<string, number>()
const registeredFloors = new Set<string>()

const FLOOR_BASE_LIGHTS: Record<string, number> = {
  F1: 0.92,
  F2: 0.56,
  F3: 0.32,
}

function getStore() {
  if (!cachedWorldStore) {
    try {
      cachedWorldStore = useWorldStore()
    } catch (error) {
      ;(window as Window & { __storeError?: string }).__storeError = String(error)
      return null
    }
  }
  return cachedWorldStore
}

/**
 * 设备动画需要和 UI 面板共享同一套楼层归属规则，否则会出现“控制的是 F1，亮的是 F2”的错位。
 */
function inferFloorFromDevice(deviceId: string, roomId?: string | null): string | null {
  const mappedFloor = getFloorForDevice(deviceId, roomId)
  if (mappedFloor) {
    return mappedFloor
  }

  const firstFloor = [...registeredFloors][0]
  return firstFloor ?? null
}

export function registerDeviceNodes(floorId: string, scene: THREE.Group) {
  const mats: THREE.ShaderMaterial[] = []
  const curtains: THREE.Object3D[] = []
  const hvacs: THREE.Object3D[] = []

  scene.traverse((obj) => {
    if ((obj as THREE.Mesh).isMesh) {
      const mat = (obj as THREE.Mesh).material
      if (mat && (mat as THREE.ShaderMaterial).isShaderMaterial && (mat as THREE.ShaderMaterial).uniforms?.u_lightIntensity) {
        mats.push(mat as THREE.ShaderMaterial)
      }
    }

    const name = obj.name.toLowerCase()
    if (name === 'curtain' || name.startsWith('curtain')) {
      curtains.push(obj)
    }
    if (name.startsWith('ac') || name.startsWith('air')) {
      hvacs.push(obj)
    }
  })

  floorMaterials.set(floorId, mats)
  curtainNodes.set(floorId, curtains)
  hvacNodes.set(floorId, hvacs)
  lightCurrents.set(floorId, FLOOR_BASE_LIGHTS[floorId] ?? 0.6)
  registeredFloors.add(floorId)
}

export function setupLightWatchers() {
  // 当前灯光强度全部走逐帧插值，不需要额外 watch。
}

export function initDeviceAnimStore(store: ReturnType<typeof useWorldStore>) {
  cachedWorldStore = store
}

export function updateDeviceAnimations(dt: number) {
  const store = getStore()
  if (!store) return

  const floorTargets = new Map<string, number>()
  for (const floorId of floorMaterials.keys()) {
    floorTargets.set(floorId, FLOOR_BASE_LIGHTS[floorId] ?? 0.55)
  }

  for (const [deviceId, device] of Object.entries(store.devices)) {
    if (device.type !== 'light') continue
    const floorId = inferFloorFromDevice(deviceId, device.location.room)
    if (!floorId || !floorMaterials.has(floorId)) continue

    const intensity = device.state.power
      ? Math.max(0.18, (device.state.extra.brightness ?? 50) / 100)
      : 0.05
    floorTargets.set(floorId, intensity)
  }

  for (const [floorId, mats] of floorMaterials) {
    const target = floorTargets.get(floorId) ?? FLOOR_BASE_LIGHTS[floorId] ?? 0.55
    const current = lightCurrents.get(floorId) ?? target
    const next = THREE.MathUtils.lerp(current, target, Math.min(4.5 * dt, 1))
    lightCurrents.set(floorId, next)

    for (const mat of mats) {
      mat.uniforms.u_lightIntensity.value = next
    }
  }

  for (const [deviceId, device] of Object.entries(store.devices)) {
    if (device.type !== 'curtain') continue
    const floorId = inferFloorFromDevice(deviceId, device.location.room)
    if (!floorId) continue

    const openPct = device.state.extra.open_percent ?? 0
    const targetScale = THREE.MathUtils.lerp(1.0, 0.15, openPct / 100)

    const floorCurtains = curtainNodes.get(floorId) ?? []
    for (const node of floorCurtains) {
      node.traverse((child) => {
        if (child.name.toLowerCase().includes('curtain0')) {
          child.scale.z = THREE.MathUtils.lerp(child.scale.z, targetScale, 2.2 * dt)
        }
      })
    }
  }

  for (const [deviceId, device] of Object.entries(store.devices)) {
    if (device.type !== 'hvac') continue
    const floorId = inferFloorFromDevice(deviceId, device.location.room)
    if (!floorId) continue

    const floorHvacs = hvacNodes.get(floorId) ?? []
    const isOn = device.state.power
    const mode = device.state.extra.mode

    for (const node of floorHvacs) {
      node.traverse((child) => {
        if (!(child as THREE.Mesh).isMesh) return
        const mat = (child as THREE.Mesh).material
        if (!mat) return
        if ((mat as THREE.ShaderMaterial).isShaderMaterial) return

        const stdMat = mat as THREE.MeshStandardMaterial
        if (!stdMat.emissive) return

        if (isOn) {
          stdMat.emissive.set(mode === 'heat' ? 0xef8a6d : 0x66c6ff)
          stdMat.emissiveIntensity = THREE.MathUtils.lerp(stdMat.emissiveIntensity, 0.46, 3.2 * dt)
        } else {
          stdMat.emissiveIntensity = THREE.MathUtils.lerp(stdMat.emissiveIntensity, 0.02, 3.2 * dt)
        }
      })
    }
  }
}
