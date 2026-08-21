export interface CameraPreset {
  springLength: number
  lookAt: [number, number, number]
  theta: number
  phi: number
  fov?: number
  smoothing?: number
  rotateSmoothing?: number
}
