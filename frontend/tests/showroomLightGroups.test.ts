import * as THREE from 'three'

import { buildShowroomLightEntries } from '../src/utils/showroomLightGroups.ts'

test('buildShowroomLightEntries 复用共享 uniform 引用，楼层位移后 shader 位置会同步更新', () => {
  const shared = [new THREE.Vector4(-4, 18, -1.5, 1)]
  const lights = buildShowroomLightEntries(
    [[-4, 0, -1.5]],
    shared,
    [1.22, 0.95, 0.82],
    2.35,
    0.5,
  )

  expect(lights[0].position).toBe(shared[0])

  shared[0].y = 35
  expect(lights[0].position.y).toBe(35)
})
