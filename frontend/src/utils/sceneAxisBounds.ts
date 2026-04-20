import * as THREE from 'three'

export interface AxisBounds {
  min: number
  max: number
}

export type BindingAxis = 'x' | 'y' | 'z'

const BOX_CORNERS = [
  new THREE.Vector3(),
  new THREE.Vector3(),
  new THREE.Vector3(),
  new THREE.Vector3(),
  new THREE.Vector3(),
  new THREE.Vector3(),
  new THREE.Vector3(),
  new THREE.Vector3(),
]

const targetInverseMatrix = new THREE.Matrix4()
const objectToTargetMatrix = new THREE.Matrix4()

function readAxisValue(vector: THREE.Vector3, axis: BindingAxis) {
  return axis === 'x' ? vector.x : axis === 'y' ? vector.y : vector.z
}

function writeBoxCorners(box: THREE.Box3) {
  const { min, max } = box
  BOX_CORNERS[0].set(min.x, min.y, min.z)
  BOX_CORNERS[1].set(min.x, min.y, max.z)
  BOX_CORNERS[2].set(min.x, max.y, min.z)
  BOX_CORNERS[3].set(min.x, max.y, max.z)
  BOX_CORNERS[4].set(max.x, min.y, min.z)
  BOX_CORNERS[5].set(max.x, min.y, max.z)
  BOX_CORNERS[6].set(max.x, max.y, min.z)
  BOX_CORNERS[7].set(max.x, max.y, max.z)
}

function collectBoundsInTargetSpace(
  object: THREE.Object3D,
  target: THREE.Object3D,
  axis: BindingAxis,
): AxisBounds | null {
  object.updateWorldMatrix(true, true)
  target.updateWorldMatrix(true, false)

  targetInverseMatrix.copy(target.matrixWorld).invert()

  let min = Infinity
  let max = -Infinity

  object.traverse((child) => {
    if (!(child instanceof THREE.Mesh)) return

    const geometry = child.geometry
    if (!(geometry instanceof THREE.BufferGeometry)) return

    if (!geometry.boundingBox) {
      geometry.computeBoundingBox()
    }

    const boundingBox = geometry.boundingBox
    if (!boundingBox) return

    objectToTargetMatrix.multiplyMatrices(targetInverseMatrix, child.matrixWorld)
    writeBoxCorners(boundingBox)

    for (const corner of BOX_CORNERS) {
      corner.applyMatrix4(objectToTargetMatrix)
      const value = readAxisValue(corner, axis)
      min = Math.min(min, value)
      max = Math.max(max, value)
    }
  })

  if (!Number.isFinite(min) || !Number.isFinite(max)) {
    return null
  }

  return { min, max }
}

/**
 * 轨道需要在自身局部空间取边界，这样拿到的 z 范围才和布帘节点的本地位移方向一致。
 */
export function getObjectAxisBoundsInLocalSpace(
  object: THREE.Object3D,
  axis: BindingAxis,
): AxisBounds | null {
  return collectBoundsInTargetSpace(object, object, axis)
}

/**
 * 布帘位移写在 parent-local position 上，所以面板边界也必须投到父节点坐标系里。
 */
export function getObjectAxisBoundsInParentSpace(
  object: THREE.Object3D,
  axis: BindingAxis,
): AxisBounds | null {
  if (!object.parent) return null
  return collectBoundsInTargetSpace(object, object.parent, axis)
}
