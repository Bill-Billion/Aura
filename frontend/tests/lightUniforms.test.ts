import test from 'node:test'
import assert from 'node:assert/strict'
import * as THREE from 'three'

import { useLightUniforms } from '../src/composables/useLightUniforms.ts'

test('initFloor 会把楼层位移写入灯光 uniform，避免高楼层继续留在 y=0', () => {
  const lights = useLightUniforms()
  const uniforms = lights.initFloor({
    floorId: 'F2',
    numLights: 2,
    positions: [[-4, 0, -1.5], [3, 0, -2.5]],
    floorY: 18,
  })

  assert.deepEqual(
    uniforms.map((uniform) => uniform.toArray()),
    [
      [-4, 18, -1.5, 1],
      [3, 18, -2.5, 1],
    ],
  )
})

test('楼层在总览态和单层态之间移动时，灯光 uniform 会跟着场景位置同步', () => {
  const lights = useLightUniforms()
  lights.initFloor({
    floorId: 'F3',
    numLights: 1,
    positions: [[-1, 0, 3]],
    floorY: 13.3,
  })

  lights.setFloorTransform('F3', new THREE.Vector3(0, 35, 0))

  assert.deepEqual(
    lights.getFloorUniforms('F3').map((uniform) => uniform.toArray()),
    [[-1, 35, 3, 1]],
  )
})
