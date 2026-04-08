import * as THREE from 'three'
import type { FloorConfig, TaggedNode, NodeCategory, DeviceNodeMapping } from '@/types/model-types'

const registry = new Map<string, TaggedNode[]>()
const deviceIndex = new Map<string, TaggedNode>()

export function useModelRegistry() {

  function registerFloor(floorId: string, scene: THREE.Group, config: FloorConfig) {
    const nodes: TaggedNode[] = []

    scene.traverse((obj) => {
      if (!(obj instanceof THREE.Mesh)) return

      const name = obj.name.toLowerCase()
      let category: NodeCategory = 'structure'
      let deviceMapping: DeviceNodeMapping | undefined

      // Check against device map first
      for (const [, mapping] of Object.entries(config.deviceMap)) {
        if (name.includes(mapping.meshName.toLowerCase())) {
          category = 'device'
          deviceMapping = mapping
          break
        }
      }

      // Auto-classify by name patterns
      if (!deviceMapping) {
        if (name.includes('glass') || name.includes('window') || name.includes('transparent')) {
          category = 'glass'
        } else if (name.includes('floor') || name.includes('ground') || name.includes('tile')) {
          category = 'floor_surface'
        } else if (name.includes('light') || name.includes('lamp')) {
          category = 'light_area'
        }
      }

      const node: TaggedNode = {
        mesh: obj,
        category,
        floorId,
        deviceMapping,
        originalMaterial: Array.isArray(obj.material) ? obj.material[0] : obj.material,
      }

      nodes.push(node)

      if (deviceMapping) {
        deviceIndex.set(deviceMapping.deviceId, node)
      }
    })

    registry.set(floorId, nodes)
    return nodes
  }

  function getNodesByCategory(floorId: string, category: NodeCategory): TaggedNode[] {
    const floorNodes = floorId === '*'
      ? [...registry.values()].flat()
      : registry.get(floorId) ?? []
    return floorNodes.filter(n => n.category === category)
  }

  function getDeviceNode(deviceId: string): TaggedNode | undefined {
    return deviceIndex.get(deviceId)
  }

  function getAllTaggedNodes(): TaggedNode[] {
    return [...registry.values()].flat()
  }

  function getFloorNodes(floorId: string): TaggedNode[] {
    return registry.get(floorId) ?? []
  }

  return {
    registerFloor,
    getNodesByCategory,
    getDeviceNode,
    getAllTaggedNodes,
    getFloorNodes,
  }
}
