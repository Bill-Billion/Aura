import * as THREE from 'three'

export interface ShowroomLightEntry {
  position: THREE.Vector4
  size: THREE.Vector3
}

/**
 * 保持 position uniform 的引用稳定，楼层升降时才能把世界坐标同步进 shader。
 */
export function buildShowroomLightEntries(
  positions: Array<[number, number, number]>,
  sharedUniforms: THREE.Vector4[],
  baseSize: [number, number, number],
  volumeScale: number,
  fallbackWeight: number,
): ShowroomLightEntry[] {
  return positions.map((position, index) => ({
    position: sharedUniforms[index] ?? new THREE.Vector4(position[0], position[1], position[2], fallbackWeight),
    size: new THREE.Vector3(
      baseSize[0] * volumeScale,
      baseSize[1] * volumeScale,
      baseSize[2] * volumeScale,
    ),
  }))
}
