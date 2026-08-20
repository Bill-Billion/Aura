
import { showSceneFloorLabels } from '../src/config/sceneOverlayConfig.ts'

test('showSceneFloorLabels 默认关闭，避免楼层信息牌遮挡 3D 场景', () => {
  expect(showSceneFloorLabels).toBe(false)
})
