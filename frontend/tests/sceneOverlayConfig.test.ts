import test from 'node:test'
import assert from 'node:assert/strict'

import { showSceneFloorLabels } from '../src/config/sceneOverlayConfig.ts'

test('showSceneFloorLabels 默认关闭，避免楼层信息牌遮挡 3D 场景', () => {
  assert.equal(showSceneFloorLabels, false)
})
