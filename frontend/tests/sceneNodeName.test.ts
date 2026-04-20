import test from 'node:test'
import assert from 'node:assert/strict'

import {
  normalizeSceneNodeName,
  normalizeSceneNodeNames,
} from '../src/utils/sceneNodeName.ts'

test('normalizeSceneNodeName 会把 Blender 导出的重复节点名归一到 three 实际使用的 key', () => {
  assert.equal(normalizeSceneNodeName('curtain01.001'), 'curtain01001')
  assert.equal(normalizeSceneNodeName(' VisualCone1 '), 'visualcone1')
})

test('normalizeSceneNodeNames 去重并过滤空值', () => {
  assert.deepEqual(
    normalizeSceneNodeNames([' curtain01.001 ', 'curtain01001', '', 'CAM2']),
    ['curtain01001', 'cam2'],
  )
})
