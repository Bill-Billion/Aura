import type * as THREE from 'three'

// ======= Floor Configuration =======

export interface FloorConfig {
  id: 'F1' | 'F2' | 'F3'
  modelPath: string
  collapsedY: number
  expandedY: number
  numLights: number
  lights: SDFLightConfig[]
  deviceMap: Record<string, DeviceNodeMapping>
}

export interface SDFLightConfig {
  position: [number, number, number]
  intensity: number
}

export interface DeviceNodeMapping {
  meshName: string
  deviceId: string
  type: 'light' | 'curtain' | 'fan' | 'hvac' | 'sensor' | 'camera' | 'other'
  lightIndex?: number
}

// ======= Model Registry =======

export type NodeCategory = 'structure' | 'device' | 'light_area' | 'glass' | 'floor_surface'

export interface TaggedNode {
  mesh: THREE.Mesh
  category: NodeCategory
  floorId: string
  deviceMapping?: DeviceNodeMapping
  originalMaterial?: THREE.Material
}

// ======= SDF Light State =======

export interface SDFLightState {
  position: THREE.Vector4  // xyz=worldPos, w=currentIntensity(0-1)
  size: THREE.Vector3
  targetIntensity: number
}

// ======= Camera Presets =======

export interface CameraPreset {
  springLength: number
  lookAt: [number, number, number]
  theta: number
  phi: number
  fov?: number
  smoothing?: number
  rotateSmoothing?: number
}

// ======= Scene Config =======

export interface GamemcuSceneConfig {
  id: string
  name: string
  floors: FloorConfig[]
  camera: {
    overview: CameraPreset
    floorFocus: CameraPreset
    floorOffset: [number, number, number]
  }
  textures: {
    matcapRoughness: string
    matcapReflection: string
    hdr: string
  }
  lightsInfo: {
    smoothMin: number
    smoothMax: number
    cornerRadius: number
  }
  lightSize: [number, number, number]
}
