import * as THREE from 'three'
import { useWorldStore } from '@/stores/worldStore'

// Cached store reference
let cachedWorldStore: any = null

function getStore() {
  if (!cachedWorldStore) {
    try {
      cachedWorldStore = useWorldStore()
    } catch (e) {
      ;(window as any).__storeError = String(e)
      return null
    }
  }
  return cachedWorldStore
}

// Floor shader materials (for u_lightIntensity)
const floorMaterials = new Map<string, THREE.ShaderMaterial[]>()
// HVAC nodes
const hvacNodes = new Map<string, THREE.Object3D[]>()
// Curtain nodes
const curtainNodes = new Map<string, THREE.Object3D[]>()
// Light intensity state
const lightCurrents = new Map<string, number>()

// Device → floor mapping (auto-populated from registered floors)
const DEVICE_FLOOR_MAP: Record<string, string> = {
  light_living_01: 'F1',
  light_bedroom_01: 'F2',
  curtain_living_01: 'F1',
  ac_living_01: 'F1',
}

// Track which floors have been registered
const registeredFloors = new Set<string>()

/**
 * Derive floor from device ID using naming convention.
 * Matches patterns like: L1_light_01, light_living_01 (defaults to first registered floor)
 */
function inferFloorFromDevice(deviceId: string): string | null {
  // Check explicit map first
  if (DEVICE_FLOOR_MAP[deviceId]) return DEVICE_FLOOR_MAP[deviceId]
  // Try L-prefix pattern: L1_xxx → F1, L2_xxx → F2
  const match = deviceId.match(/^L(\d)_/)
  if (match) return `F${match[1]}`
  // Fallback: assign to first registered floor
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
      if (mat && (mat as THREE.ShaderMaterial).isShaderMaterial && (mat as any).uniforms?.u_lightIntensity) {
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
  lightCurrents.set(floorId, 1.0)
  registeredFloors.add(floorId)
  console.log(`[DeviceAnim] ${floorId}: ${mats.length} shader mats, ${curtains.length} curtains, ${hvacs.length} hvacs`)
}

// No watchers needed — we read worldStore directly every frame
export function setupLightWatchers() {
  // No-op: logic moved to updateDeviceAnimations
}

export function initDeviceAnimStore(store: any) {
  cachedWorldStore = store
}

/**
 * Per-frame: read worldStore → compute targets → lerp → update uniforms
 */
export function updateDeviceAnimations(dt: number) {
  const store = getStore()
  if (!store) return

  // Debug: expose state to window
  ;(window as any).__deviceAnimDebug = {
    floorMatCounts: Object.fromEntries([...floorMaterials].map(([k, v]) => [k, v.length])),
    lightCurrents: Object.fromEntries(lightCurrents),
  }

  // === LIGHTS: read device state directly, compute target per floor ===
  const floorTargets = new Map<string, number>()

  // Default: all lights full on
  for (const floorId of floorMaterials.keys()) {
    floorTargets.set(floorId, 1.0)
  }

  // Override with actual device states
  // Iterate ALL devices, not just hardcoded map
  for (const [deviceId, device] of Object.entries(store.devices)) {
    if (device.type !== 'light') continue
    const floorId = inferFloorFromDevice(deviceId)
    if (!floorId || !floorMaterials.has(floorId)) continue
    const intensity = device.state.power
      ? Math.max(0.1, (device.state.extra.brightness ?? 50) / 100)
      : 0
    floorTargets.set(floorId, intensity)
  }

  // Lerp and update uniforms
  for (const [floorId, mats] of floorMaterials) {
    const target = floorTargets.get(floorId) ?? 1.0
    const current = lightCurrents.get(floorId) ?? 1.0
    const next = THREE.MathUtils.lerp(current, target, Math.min(5 * dt, 1))
    lightCurrents.set(floorId, next)

    for (const mat of mats) {
      mat.uniforms.u_lightIntensity.value = next
    }
  }

  // === CURTAINS: iterate all curtain devices ===
  for (const [deviceId, device] of Object.entries(store.devices)) {
    if (device.type !== 'curtain') continue
    const floorId = inferFloorFromDevice(deviceId)
    if (!floorId) continue
    const openPct = device.state.extra.open_percent ?? 0
    const targetScale = THREE.MathUtils.lerp(1.0, 0.15, openPct / 100)

    const floorCurtains = curtainNodes.get(floorId) ?? []
    for (const node of floorCurtains) {
      node.traverse((child) => {
        if (child.name.toLowerCase().includes('curtain0')) {
          child.scale.z = THREE.MathUtils.lerp(child.scale.z, targetScale, 2 * dt)
        }
      })
    }
  }

  // === HVAC: update emissive based on power/mode ===
  for (const [deviceId, device] of Object.entries(store.devices)) {
    if (device.type !== 'hvac') continue
    const floorId = inferFloorFromDevice(deviceId)
    if (!floorId) continue
    const floorHvacs = hvacNodes.get(floorId) ?? []
    const isOn = device.state.power
    const mode = device.state.extra.mode
    for (const node of floorHvacs) {
      node.traverse((child) => {
        if (!(child as THREE.Mesh).isMesh) return
        const mat = (child as THREE.Mesh).material
        if (!mat) return
        // ShaderMaterial (from applyShaderMaterials) has no emissive — skip
        if ((mat as THREE.ShaderMaterial).isShaderMaterial) return
        const stdMat = mat as THREE.MeshStandardMaterial
        if (!stdMat.emissive) return
        if (isOn) {
          stdMat.emissive.set(mode === 'heat' ? 0xef5350 : 0x4fc3f7)
          stdMat.emissiveIntensity = THREE.MathUtils.lerp(stdMat.emissiveIntensity, 0.4, 3 * dt)
        } else {
          stdMat.emissiveIntensity = THREE.MathUtils.lerp(stdMat.emissiveIntensity, 0, 3 * dt)
        }
      })
    }
  }
}
