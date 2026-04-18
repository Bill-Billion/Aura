import type { CameraPreset, GamemcuSceneConfig } from '@/types/model-types'

export function useCameraPresets(config: GamemcuSceneConfig) {

  const overview: CameraPreset = config.camera.overview

  function floorPreset(floorId: string): CameraPreset {
    const floor = config.floors.find(f => f.id === floorId)
    if (!floor) return overview

    const base = config.camera.floorFocus
    const offset = config.camera.floorOffset

    return {
      ...base,
      lookAt: [
        offset[0],
        floor.collapsedY + offset[1],
        offset[2],
      ],
    }
  }

  function devicePreset(_deviceId: string): CameraPreset {
    // Default close-up preset
    return {
      springLength: 14,
      lookAt: [0, -0.15, 0],
      theta: -10.3,
      phi: 1,
      smoothing: 4,
      rotateSmoothing: 4,
    }
  }

  return {
    overview,
    floorPreset,
    devicePreset,
  }
}
