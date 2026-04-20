import type { DeviceState } from '@/types/world-state'
import { normalizeSceneNodeName, normalizeSceneNodeNames } from '@/utils/sceneNodeName'

export type BindingAxis = 'x' | 'y' | 'z'

export interface SceneBindingPanelNode {
  node: string
  side: 'left' | 'right'
  axis: BindingAxis
}

export interface NormalizedSceneBindings {
  animation: string | null
  pickNodes: string[]
  bodyNodes: string[]
  effectNodes: string[]
  coneNodes: string[]
  trackNodes: string[]
  panelNodes: SceneBindingPanelNode[]
  headNodes: string[]
  rotorNodes: string[]
  rotorAxis: BindingAxis
  shakeAxis: BindingAxis
}

const EMPTY_BINDINGS: NormalizedSceneBindings = {
  animation: null,
  pickNodes: [],
  bodyNodes: [],
  effectNodes: [],
  coneNodes: [],
  trackNodes: [],
  panelNodes: [],
  headNodes: [],
  rotorNodes: [],
  rotorAxis: 'z',
  shakeAxis: 'y',
}

function readAxis(value: unknown, fallback: BindingAxis): BindingAxis {
  return value === 'x' || value === 'y' || value === 'z' ? value : fallback
}

function readNodeList(value: unknown): string[] {
  if (!Array.isArray(value)) return []
  return normalizeSceneNodeNames(value.filter((item): item is string => typeof item === 'string'))
}

function readPanelNodes(value: unknown): SceneBindingPanelNode[] {
  if (!Array.isArray(value)) return []

  return value.flatMap((item) => {
    if (!item || typeof item !== 'object') return []
    const entry = item as Record<string, unknown>
    const rawNode = entry.node
    const node = typeof rawNode === 'string'
      ? normalizeSceneNodeName(rawNode)
      : ''
    const side = entry.side === 'right' ? 'right' : 'left'
    if (!node) return []

    return [{
      node,
      side,
      axis: readAxis(entry.axis, 'z'),
    }]
  })
}

/**
 * 后端注册表会把场景节点描述透给前端，这里统一做一次清洗，
 * 避免渲染层和动画层各自猜字段，最后把同一台设备绑到不同节点上。
 */
export function getSceneBindings(device: DeviceState | null | undefined): NormalizedSceneBindings {
  if (!device) return EMPTY_BINDINGS

  const raw = device.scene_bindings ?? {}
  const legacyNodes = readNodeList(raw.glb_nodes)

  return {
    animation: typeof raw.animation === 'string' ? raw.animation : null,
    pickNodes: readNodeList(raw.pick_nodes).length > 0 ? readNodeList(raw.pick_nodes) : legacyNodes,
    bodyNodes: readNodeList(raw.body_nodes).length > 0 ? readNodeList(raw.body_nodes) : legacyNodes,
    effectNodes: readNodeList(raw.effect_nodes),
    coneNodes: readNodeList(raw.cone_nodes),
    trackNodes: readNodeList(raw.track_nodes),
    panelNodes: readPanelNodes(raw.panel_nodes),
    headNodes: readNodeList(raw.head_nodes),
    rotorNodes: readNodeList(raw.rotor_nodes),
    rotorAxis: readAxis(raw.rotor_axis, 'z'),
    shakeAxis: readAxis(raw.shake_axis, 'y'),
  }
}

export function getSelectableBindingNodes(bindings: NormalizedSceneBindings): string[] {
  return normalizeSceneNodeNames([
    ...bindings.pickNodes,
    ...bindings.bodyNodes,
  ])
}
