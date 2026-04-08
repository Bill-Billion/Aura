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
// Curtain nodes
const curtainNodes = new Map<string, THREE.Object3D[]>()
// Light intensity state
const lightCurrents = new Map<string, number>()

// Device → floor mapping
const DEVICE_FLOOR_MAP: Record<string, string> = {
  light_living_01: 'F1',
  light_bedroom_01: 'F2',
  curtain_living_01: 'F1',
}

export function registerDeviceNodes(floorId: string, scene: THREE.Group) {
  const mats: THREE.ShaderMaterial[] = []
  const curtains: THREE.Object3D[] = []

  scene.traverse((obj) => {
    if ((obj as THREE.Mesh).isMesh) {
      const mat = (obj as THREE.Mesh).material
      if (mat && (mat as THREE.ShaderMaterial).isShaderMaterial && (mat as any).uniforms?.u_lightIntensity) {
        mats.push(mat as THREE.ShaderMaterial)
      }
    }
    const name = obj.name.toLowerCase()
    if (name === 'curtain' || name === 'curtain1' || name === 'curtain2') {
      curtains.push(obj)
    }
  })

  floorMaterials.set(floorId, mats)
  curtainNodes.set(floorId, curtains)
  lightCurrents.set(floorId, 1.0)
  console.log(`[DeviceAnim] ${floorId}: ${mats.length} shader mats, ${curtains.length} curtains`)
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
  for (const [deviceId, floorId] of Object.entries(DEVICE_FLOOR_MAP)) {
    const device = store.devices[deviceId]
    if (!device || device.type !== 'light') continue
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

  // === CURTAINS: lerp scale ===
  const curtainDevice = store.devices['curtain_living_01']
  if (curtainDevice) {
    const openPct = curtainDevice.state.extra.open_percent ?? 0
    const targetScale = THREE.MathUtils.lerp(1.0, 0.15, openPct / 100)

    const f1Curtains = curtainNodes.get('F1') ?? []
    for (const node of f1Curtains) {
      node.traverse((child) => {
        if (child.name.toLowerCase().includes('curtain0')) {
          child.scale.z = THREE.MathUtils.lerp(child.scale.z, targetScale, 2 * dt)
        }
      })
    }
  }
}
