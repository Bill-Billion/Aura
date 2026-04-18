import type { CameraPreset } from '@/types/model-types'

export type ShowroomMaterialRole =
  | 'wallGlass'
  | 'floorDeck'
  | 'furniture'
  | 'applianceMetal'
  | 'vehicleFx'
  | 'signage'
  | 'reflection'

interface FloorVisualConfig {
  id: 'F1' | 'F2' | 'F3'
  name: string
  collapsedY: number
  expandedY: number
  lightBias: number
  envBias: number
  labelAnchor: [number, number, number]
  lights: [number, number, number][]
}

interface ShowroomGroundConfig {
  planeY: number
  reflectionPlaneY: number
  size: number
}

interface OverlayConfig {
  mode: 'showroom'
  panelWidth: number
}

interface ShowroomVisualConfig {
  overlay: OverlayConfig
  ground: ShowroomGroundConfig
  camera: {
    overview: CameraPreset
    floors: Record<'F1' | 'F2' | 'F3', CameraPreset>
  }
  floors: Record<'F1' | 'F2' | 'F3', FloorVisualConfig>
  materialPalette: Record<ShowroomMaterialRole, number>
}

export const showroomVisualConfig: ShowroomVisualConfig = {
  overlay: {
    mode: 'showroom',
    panelWidth: 312,
  },
  ground: {
    planeY: -0.04,
    reflectionPlaneY: -0.08,
    size: 220,
  },
  camera: {
    overview: {
      springLength: 118,
      lookAt: [-4.4, 6.4, 1.4],
      theta: 2.35 + Math.PI,
      phi: 1.22,
      fov: 12,
      smoothing: 4.4,
      rotateSmoothing: 4.7,
    },
    floors: {
      F1: {
        springLength: 44,
        lookAt: [-1.1, 1.9, 0.5],
        theta: 2.2 + Math.PI,
        phi: 0.92,
        fov: 12.1,
        smoothing: 4.5,
        rotateSmoothing: 4.7,
      },
      F2: {
        springLength: 47,
        lookAt: [-1.1, 18.9, 0.5],
        theta: 2.22 + Math.PI,
        phi: 0.91,
        fov: 12.3,
        smoothing: 4.5,
        rotateSmoothing: 4.7,
      },
      F3: {
        springLength: 50,
        lookAt: [-0.8, 35.0, 0.7],
        theta: 2.26 + Math.PI,
        phi: 0.93,
        fov: 12.5,
        smoothing: 4.5,
        rotateSmoothing: 4.7,
      },
    },
  },
  floors: {
    F1: {
      id: 'F1',
      name: 'Living Deck',
      collapsedY: 0.9,
      expandedY: 0,
      lightBias: 1.08,
      envBias: 1.14,
      labelAnchor: [9.0, 2.15, 2.2],
      lights: [[-2, 0, -1.5], [-2, 0, 1], [-1, 0, 4]],
    },
    F2: {
      id: 'F2',
      name: 'Private Deck',
      collapsedY: 7.2,
      expandedY: 18,
      lightBias: 0.5,
      envBias: 0.86,
      labelAnchor: [8.0, 2.0, 2.6],
      lights: [[-4, 0, -1.5], [3, 0, -2.5]],
    },
    F3: {
      id: 'F3',
      name: 'Service Deck',
      collapsedY: 13.3,
      expandedY: 35,
      lightBias: 0.24,
      envBias: 0.76,
      labelAnchor: [7.4, 2.0, 2.0],
      lights: [[-1, 0, 3], [3, 0, -2.5]],
    },
  },
  materialPalette: {
    wallGlass: 0x6b8590,
    floorDeck: 0x242d37,
    furniture: 0x2c3440,
    applianceMetal: 0x48515d,
    vehicleFx: 0x78828f,
    signage: 0xb7bec9,
    reflection: 0x181d25,
  },
}
