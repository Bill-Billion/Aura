import * as THREE from 'three'

import {
  getObjectAxisBoundsInLocalSpace,
  getObjectAxisBoundsInParentSpace,
} from '../src/utils/sceneAxisBounds.ts'

test('getObjectAxisBoundsInLocalSpace 会在节点自身坐标系里汇总整个子树的边界', () => {
  const track = new THREE.Group()
  const leftPanel = new THREE.Mesh(new THREE.BoxGeometry(0.024, 2.1, 1.241))
  const rightPanel = new THREE.Mesh(new THREE.BoxGeometry(0.024, 2.1, 1.241))

  leftPanel.position.z = 1.261
  rightPanel.position.z = -1.231
  track.add(leftPanel)
  track.add(rightPanel)
  track.updateMatrixWorld(true)

  const bounds = getObjectAxisBoundsInLocalSpace(track, 'z')

  assert.ok(bounds)
  expect(Math.abs(bounds.min - (-1.8515))).toBeLessThan(1e-6)
  expect(Math.abs(bounds.max - 1.8815)).toBeLessThan(1e-6)
})

test('getObjectAxisBoundsInParentSpace 会保留父节点本地 z 方向，不受世界坐标旋转影响', () => {
  const root = new THREE.Group()
  const curtain = new THREE.Group()
  const panel = new THREE.Mesh(new THREE.BoxGeometry(0.024, 2.1, 1.241))

  curtain.rotation.y = Math.PI / 2
  panel.position.z = 1.261
  curtain.add(panel)
  root.add(curtain)
  root.updateMatrixWorld(true)

  const bounds = getObjectAxisBoundsInParentSpace(panel, 'z')

  assert.ok(bounds)
  expect(Math.abs(bounds.min - 0.6405)).toBeLessThan(1e-6)
  expect(Math.abs(bounds.max - 1.8815)).toBeLessThan(1e-6)
})
