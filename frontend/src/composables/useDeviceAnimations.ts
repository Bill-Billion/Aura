import * as THREE from 'three'
import { useWorldStore } from '@/stores/worldStore'
import { useUIStore } from '@/stores/uiStore'
import type { DeviceState } from '@/types/world-state'
import { getSceneBindings, type BindingAxis } from '@/utils/sceneBindings'
import {
  computeCameraConeOpacity,
  computeCurtainPanelPose,
  type AxisBounds,
} from '@/utils/deviceAnimationMath'
import {
  getObjectAxisBoundsInLocalSpace,
  getObjectAxisBoundsInParentSpace,
} from '@/utils/sceneAxisBounds'
import { normalizeSceneNodeName } from '@/utils/sceneNodeName'
import { getFloorForDevice } from '@/utils/deviceFloorMap'

let cachedWorldStore: ReturnType<typeof useWorldStore> | null = null
let cachedUIStore: ReturnType<typeof useUIStore> | null = null

const floorMaterials = new Map<string, THREE.ShaderMaterial[]>()
const floorNodeLookup = new Map<string, Map<string, THREE.Object3D[]>>()
const lightCurrents = new Map<string, number>()
const registeredFloors = new Set<string>()
const warnedAnimationCoverage = new Set<string>()
const baseNodeState = new WeakMap<THREE.Object3D, {
  position: THREE.Vector3
  rotation: THREE.Euler
  scale: THREE.Vector3
}>()

let animationTime = 0

const FLOOR_BASE_LIGHTS: Record<string, number> = {
  F1: 0.92,
  F2: 0.56,
  F3: 0.32,
}

const FLOOR_LIGHT_GAIN: Record<string, number> = {
  F1: 1,
  F2: 1.15,
  F3: 1.2,
}

const FAN_SPEED_MAP: Record<string, number> = {
  low: 7,
  medium: 12,
  high: 18,
}

const SUPPORTED_ANIMATIONS = new Set(['curtain', 'fan', 'camera', 'hvac'])

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

function getUI() {
  if (!cachedUIStore) {
    try {
      cachedUIStore = useUIStore()
    } catch (error) {
      ;(window as Window & { __uiStoreError?: string }).__uiStoreError = String(error)
      return null
    }
  }
  return cachedUIStore
}

/**
 * 设备动画需要和 UI 面板共享同一套楼层归属规则，否则会出现“控制的是 F1，亮的是 F2”的错位。
 */
function inferFloorFromDevice(
  deviceId: string,
  roomId?: string | null,
  explicitFloorId?: string | null,
): string | null {
  const mappedFloor = getFloorForDevice(deviceId, roomId, explicitFloorId)
  if (mappedFloor) {
    return mappedFloor
  }

  const firstFloor = [...registeredFloors][0]
  return firstFloor ?? null
}

function rememberBaseState(node: THREE.Object3D) {
  let state = baseNodeState.get(node)
  if (!state) {
    state = {
      position: node.position.clone(),
      rotation: new THREE.Euler(node.rotation.x, node.rotation.y, node.rotation.z, node.rotation.order),
      scale: node.scale.clone(),
    }
    baseNodeState.set(node, state)
  }
  return state
}

function getAxisValue(target: THREE.Vector3 | THREE.Euler, axis: BindingAxis) {
  return axis === 'x' ? target.x : axis === 'y' ? target.y : target.z
}

function setAxisValue(target: THREE.Vector3 | THREE.Euler, axis: BindingAxis, value: number) {
  if (axis === 'x') {
    target.x = value
    return
  }
  if (axis === 'y') {
    target.y = value
    return
  }
  target.z = value
}

function getCachedAxisBounds(
  node: THREE.Object3D,
  cacheKey: string,
  resolver: () => AxisBounds | null,
): AxisBounds | null {
  const cached = node.userData[cacheKey]
  if (cached === null) return null
  if (
    cached
    && typeof cached === 'object'
    && typeof cached.min === 'number'
    && typeof cached.max === 'number'
  ) {
    return cached as AxisBounds
  }

  const bounds = resolver()
  node.userData[cacheKey] = bounds ?? null
  return bounds
}

function getAxisBoundsInParentSpace(node: THREE.Object3D, axis: BindingAxis): AxisBounds | null {
  return getCachedAxisBounds(
    node,
    `showroomParentBounds_${axis}`,
    () => getObjectAxisBoundsInParentSpace(node, axis),
  )
}

function warnAnimationCoverageOnce(key: string, message: string) {
  if (!import.meta.env.DEV || warnedAnimationCoverage.has(key)) return
  warnedAnimationCoverage.add(key)
  console.warn(message)
}

function getAxisBoundsInLocalSpace(node: THREE.Object3D, axis: BindingAxis): AxisBounds | null {
  return getCachedAxisBounds(
    node,
    `showroomLocalBounds_${axis}`,
    () => getObjectAxisBoundsInLocalSpace(node, axis),
  )
}

function resolveCurtainWindowCenter(
  floorId: string,
  device: DeviceState,
  axis: BindingAxis,
): number | null {
  const bindings = getSceneBindings(device)
  const trackNodes = getSceneNodesForNames(floorId, bindings.trackNodes)
  for (const node of trackNodes) {
    const bounds = getAxisBoundsInLocalSpace(node, axis)
    if (bounds) {
      return (bounds.min + bounds.max) * 0.5
    }
  }

  const panelCenters: number[] = []
  for (const panel of bindings.panelNodes.filter((entry) => entry.axis === axis)) {
    const nodes = getSceneNodesForNames(floorId, [panel.node])
    for (const node of nodes) {
      const bounds = getAxisBoundsInParentSpace(node, axis)
      if (bounds) {
        panelCenters.push((bounds.min + bounds.max) * 0.5)
      }
    }
  }

  if (panelCenters.length === 0) return null
  return panelCenters.reduce((sum, center) => sum + center, 0) / panelCenters.length
}

function ensureFxMaterial(node: THREE.Object3D, kind: 'cone' | 'effect') {
  if (!(node instanceof THREE.Mesh)) return null

  const cached = node.userData.showroomFxMaterial
  if (cached instanceof THREE.MeshBasicMaterial) {
    return cached
  }

  const material = new THREE.MeshBasicMaterial({
    color: new THREE.Color(kind === 'cone' ? 0xffe75a : 0x7cc8ff),
    transparent: true,
    opacity: 0,
    depthWrite: false,
    blending: THREE.AdditiveBlending,
    side: THREE.DoubleSide,
  })
  material.toneMapped = false

  node.material = material
  node.visible = false
  node.renderOrder = kind === 'cone' ? 8 : 7
  node.userData.showroomFxMaterial = material

  rememberBaseState(node)
  return material
}

function updateFxNodes(
  nodes: THREE.Object3D[],
  {
    kind,
    opacity,
    color,
    scalePulse = 1,
    dt,
  }: {
    kind: 'cone' | 'effect'
    opacity: number
    color: number
    scalePulse?: number
    dt: number
  },
) {
  for (const node of nodes) {
    const material = ensureFxMaterial(node, kind)
    if (!material) continue

    material.color.set(color)
    material.opacity = THREE.MathUtils.damp(material.opacity, opacity, 7, dt)
    node.visible = material.opacity > 0.02

    const base = rememberBaseState(node)
    node.scale.set(
      THREE.MathUtils.damp(node.scale.x, base.scale.x, 5, dt),
      THREE.MathUtils.damp(node.scale.y, base.scale.y * scalePulse, 5, dt),
      THREE.MathUtils.damp(node.scale.z, base.scale.z, 5, dt),
    )
  }
}

function getSceneNodesForDevice(device: DeviceState, floorId: string) {
  const bindings = getSceneBindings(device)

  return {
    bindings,
    bodyNodes: getSceneNodesForNames(floorId, bindings.bodyNodes),
    effectNodes: getSceneNodesForNames(floorId, bindings.effectNodes),
    coneNodes: getSceneNodesForNames(floorId, bindings.coneNodes),
    headNodes: getSceneNodesForNames(floorId, bindings.headNodes),
    rotorNodes: getSceneNodesForNames(floorId, bindings.rotorNodes),
  }
}

function animateCurtainDevice(device: DeviceState, floorId: string, dt: number) {
  const bindings = getSceneBindings(device)
  const openRatio = THREE.MathUtils.clamp((device.state.extra.open_percent ?? 0) / 100, 0, 1)

  if (bindings.panelNodes.length === 0) {
    warnAnimationCoverageOnce(
      `${device.id}:curtain:no-panels`,
      `[SceneBindings] ${device.display_name || device.id} 声明了 curtain 动画，但没有 panel_nodes`,
    )
    return
  }

  const windowCenters = new Map<BindingAxis, number | null>()

  for (const panel of bindings.panelNodes) {
    const nodes = getSceneNodesForNames(floorId, [panel.node])
    if (nodes.length === 0) {
      warnAnimationCoverageOnce(
        `${device.id}:curtain:missing-node:${panel.node}`,
        `[SceneBindings] ${device.display_name || device.id} 的 curtain 节点 ${panel.node} 未命中 ${floorId} 场景`,
      )
      continue
    }

    if (!windowCenters.has(panel.axis)) {
      windowCenters.set(panel.axis, resolveCurtainWindowCenter(floorId, device, panel.axis))
    }

    for (const node of nodes) {
      const base = rememberBaseState(node)
      const basePosition = getAxisValue(base.position, panel.axis)
      const baseScale = getAxisValue(base.scale, panel.axis)
      const authoredBounds = getAxisBoundsInParentSpace(node, panel.axis)
      if (!authoredBounds) {
        warnAnimationCoverageOnce(
          `${device.id}:curtain:no-bounds:${panel.node}`,
          `[SceneBindings] ${device.display_name || device.id} 的 curtain 节点 ${panel.node} 无法计算父坐标边界`,
        )
        continue
      }

      const windowCenter = windowCenters.get(panel.axis) ?? ((authoredBounds.min + authoredBounds.max) * 0.5)
      const pose = computeCurtainPanelPose({
        side: panel.side,
        authoredPosition: basePosition,
        authoredBounds,
        authoredScale: baseScale,
        windowCenter,
        openRatio,
        minGatherScale: 0.18,
      })

      setAxisValue(
        node.position,
        panel.axis,
        THREE.MathUtils.damp(getAxisValue(node.position, panel.axis), pose.position, 7, dt),
      )
      setAxisValue(
        node.scale,
        panel.axis,
        THREE.MathUtils.damp(getAxisValue(node.scale, panel.axis), pose.scale, 7, dt),
      )
    }
  }
}

function animateFanDevice(device: DeviceState, floorId: string, dt: number) {
  const { bindings, headNodes, rotorNodes } = getSceneNodesForDevice(device, floorId)
  if (headNodes.length === 0 && rotorNodes.length === 0) {
    warnAnimationCoverageOnce(
      `${device.id}:fan:no-nodes`,
      `[SceneBindings] ${device.display_name || device.id} 声明了 fan 动画，但没有命中 head/rotor 节点`,
    )
    return
  }

  const power = Boolean(device.state.power)
  const speed = String(device.state.extra.speed ?? 'low')
  const shake = Boolean(device.state.extra.shake)
  const spinVelocity = power ? (FAN_SPEED_MAP[speed] ?? FAN_SPEED_MAP.low) : 0

  for (const node of rotorNodes) {
    node.rotation[bindings.rotorAxis] += spinVelocity * dt
  }

  for (const node of headNodes) {
    const base = rememberBaseState(node)
    const baseAngle = getAxisValue(base.rotation, bindings.shakeAxis)
    const swing = power && shake ? Math.sin(animationTime * 1.6) * 0.36 : 0
    setAxisValue(
      node.rotation,
      bindings.shakeAxis,
      THREE.MathUtils.damp(getAxisValue(node.rotation, bindings.shakeAxis), baseAngle + swing, 4.5, dt),
    )
  }
}

function animateCameraDevice(
  device: DeviceState,
  floorId: string,
  dt: number,
  isSelected: boolean,
) {
  const { coneNodes } = getSceneNodesForDevice(device, floorId)
  if (coneNodes.length === 0) {
    warnAnimationCoverageOnce(
      `${device.id}:camera:no-cone`,
      `[SceneBindings] ${device.display_name || device.id} 声明了 camera 动画，但没有命中 cone 节点`,
    )
    return
  }

  const online = Boolean(device.state.extra.online ?? device.state.power)
  const pulse = 0.5 + 0.5 * Math.sin(animationTime * 2.2)
  const targetOpacity = computeCameraConeOpacity({
    isSelected,
    online,
    pulse,
  })

  updateFxNodes(coneNodes, {
    kind: 'cone',
    opacity: targetOpacity,
    color: online ? 0xffe75a : 0x7d8793,
    scalePulse: isSelected ? (online ? 1.04 + pulse * 0.03 : 1.02) : 1,
    dt,
  })
}

function animateHvacDevice(device: DeviceState, floorId: string, dt: number) {
  const { bodyNodes, effectNodes } = getSceneNodesForDevice(device, floorId)
  if (bodyNodes.length === 0 && effectNodes.length === 0) {
    warnAnimationCoverageOnce(
      `${device.id}:hvac:no-nodes`,
      `[SceneBindings] ${device.display_name || device.id} 声明了 hvac 动画，但没有命中 body/effect 节点`,
    )
    return
  }

  const isOn = Boolean(device.state.power)
  const mode = String(device.state.extra.mode ?? 'cool')
  const speed = String(device.state.extra.speed ?? 'low')
  const coolMode = mode !== 'heat'
  const bodyColor = coolMode ? 0x66c6ff : 0xef8a6d
  const effectColor = coolMode ? 0x79d7ff : 0xffb178
  const speedBoost = speed === 'high' ? 0.18 : speed === 'medium' ? 0.1 : 0.04
  const effectPulse = 0.5 + 0.5 * Math.sin(animationTime * 3.1)
  const effectOpacity = isOn ? 0.22 + speedBoost + effectPulse * 0.22 : 0

  for (const node of bodyNodes) {
    node.traverse((child) => {
      if (!(child instanceof THREE.Mesh)) return
      if (child.userData.showroomFxMaterial) return

      const material = Array.isArray(child.material) ? child.material[0] : child.material
      if (!(material instanceof THREE.MeshStandardMaterial)) return

      material.emissive.set(bodyColor)
      material.emissiveIntensity = THREE.MathUtils.damp(
        material.emissiveIntensity,
        isOn ? 0.48 : 0.02,
        4,
        dt,
      )
    })
  }

  updateFxNodes(effectNodes, {
    kind: 'effect',
    opacity: effectOpacity,
    color: effectColor,
    scalePulse: 1.03 + effectPulse * 0.05,
    dt,
  })
}

export function registerDeviceNodes(floorId: string, scene: THREE.Group) {
  const mats: THREE.ShaderMaterial[] = []
  const nodeLookup = new Map<string, THREE.Object3D[]>()

  scene.traverse((obj) => {
    if ((obj as THREE.Mesh).isMesh) {
      const mat = (obj as THREE.Mesh).material
      if (
        mat
        && (mat as THREE.ShaderMaterial).isShaderMaterial
        && (mat as THREE.ShaderMaterial).uniforms?.u_lightIntensity
      ) {
        mats.push(mat as THREE.ShaderMaterial)
      }
    }

    const name = normalizeSceneNodeName(obj.name)
    if (name) {
      const bucket = nodeLookup.get(name) ?? []
      bucket.push(obj)
      nodeLookup.set(name, bucket)
    }

    if (name.startsWith('visualcone')) {
      ensureFxMaterial(obj, 'cone')
    }
    if (name.startsWith('effect')) {
      ensureFxMaterial(obj, 'effect')
    }
  })

  floorMaterials.set(floorId, mats)
  floorNodeLookup.set(floorId, nodeLookup)
  lightCurrents.set(floorId, FLOOR_BASE_LIGHTS[floorId] ?? 0.6)
  registeredFloors.add(floorId)
}

export function getSceneNodesForNames(floorId: string, nodeNames: string[]) {
  const lookup = floorNodeLookup.get(floorId)
  if (!lookup || nodeNames.length === 0) return []

  const result: THREE.Object3D[] = []
  const seen = new Set<THREE.Object3D>()

  for (const rawName of nodeNames) {
    const bucket = lookup.get(normalizeSceneNodeName(rawName)) ?? []
    for (const node of bucket) {
      if (seen.has(node)) continue
      seen.add(node)
      result.push(node)
    }
  }

  return result
}

export function getFloorLightCurrent(floorId: string) {
  return lightCurrents.get(floorId) ?? FLOOR_BASE_LIGHTS[floorId] ?? 0.55
}

export function setupLightWatchers() {
  // 当前灯光强度全部走逐帧插值，不需要额外 watch。
}

export function initDeviceAnimStore(
  store: ReturnType<typeof useWorldStore>,
  uiStore?: ReturnType<typeof useUIStore>,
) {
  cachedWorldStore = store
  cachedUIStore = uiStore ?? cachedUIStore
}

export function updateDeviceAnimations(dt: number) {
  const store = getStore()
  if (!store) return

  animationTime += dt

  const floorTargets = new Map<string, number>()
  const floorLightCounts = new Map<string, number>()
  const floorsWithLightDevices = new Set<string>()
  for (const floorId of floorMaterials.keys()) {
    floorTargets.set(floorId, 0)
    floorLightCounts.set(floorId, 0)
  }

  for (const [deviceId, device] of Object.entries(store.devices)) {
    if (device.type !== 'light') continue
    const floorId = inferFloorFromDevice(deviceId, device.location.room, device.floor_id)
    if (!floorId || !floorMaterials.has(floorId)) continue
    floorsWithLightDevices.add(floorId)

    const gain = FLOOR_LIGHT_GAIN[floorId] ?? 1
    const intensity = device.state.power
      ? Math.max(0.12, (device.state.extra.brightness ?? 50) / 100) * gain
      : 0
    floorTargets.set(floorId, (floorTargets.get(floorId) ?? 0) + intensity)
    floorLightCounts.set(floorId, (floorLightCounts.get(floorId) ?? 0) + 1)
  }

  for (const [floorId, mats] of floorMaterials) {
    const base = FLOOR_BASE_LIGHTS[floorId] ?? 0.55
    const count = floorLightCounts.get(floorId) ?? 0
    const averageBoost = count > 0 ? (floorTargets.get(floorId) ?? 0) / count : 0
    const target = floorsWithLightDevices.has(floorId)
      ? base + averageBoost * 0.18
      : base
    const current = lightCurrents.get(floorId) ?? target
    const next = THREE.MathUtils.lerp(current, target, Math.min(4.5 * dt, 1))
    lightCurrents.set(floorId, next)

    for (const mat of mats) {
      mat.uniforms.u_lightIntensity.value = next
    }
  }

  const uiStore = getUI()

  for (const [deviceId, device] of Object.entries(store.devices)) {
    const floorId = inferFloorFromDevice(deviceId, device.location.room, device.floor_id)
    if (!floorId || !floorNodeLookup.has(floorId)) continue

    const animationKey = getSceneBindings(device).animation
    if (animationKey && !SUPPORTED_ANIMATIONS.has(animationKey)) {
      warnAnimationCoverageOnce(
        `${device.id}:unsupported-animation:${animationKey}`,
        `[SceneBindings] ${device.display_name || device.id} 声明了未处理的动画类型: ${animationKey}`,
      )
      continue
    }

    if (device.type === 'curtain') {
      animateCurtainDevice(device, floorId, dt)
      continue
    }

    if (device.type === 'fan') {
      animateFanDevice(device, floorId, dt)
      continue
    }

    if (device.type === 'camera') {
      animateCameraDevice(device, floorId, dt, uiStore?.activeDevice === deviceId)
      continue
    }

    if (device.type === 'hvac') {
      animateHvacDevice(device, floorId, dt)
    }
  }
}
