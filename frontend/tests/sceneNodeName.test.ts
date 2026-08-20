
import {
  normalizeSceneNodeName,
  normalizeSceneNodeNames,
} from '../src/utils/sceneNodeName.ts'

test('normalizeSceneNodeName 会把 Blender 导出的重复节点名归一到 three 实际使用的 key', () => {
  expect(normalizeSceneNodeName('curtain01.001')).toBe('curtain01001')
  expect(normalizeSceneNodeName(' VisualCone1 ')).toBe('visualcone1')
})

test('normalizeSceneNodeNames 去重并过滤空值', () => {
  assert.deepEqual(
    normalizeSceneNodeNames([' curtain01.001 ', 'curtain01001', '', 'CAM2']),
    ['curtain01001', 'cam2'],
  )
})
