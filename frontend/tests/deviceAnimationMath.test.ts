
import {
  computeCameraConeOpacity,
  computeCurtainPanelPose,
  type AxisBounds,
} from '../src/utils/deviceAnimationMath.ts'

function projectBoundsFromPose(
  authoredBounds: AxisBounds,
  authoredPosition: number,
  authoredScale: number,
  pose: { position: number, scale: number },
) {
  const authoredCenter = (authoredBounds.min + authoredBounds.max) * 0.5
  const authoredSpan = authoredBounds.max - authoredBounds.min
  const targetSpan = authoredSpan * (pose.scale / authoredScale)
  const targetCenter = authoredCenter + (pose.position - authoredPosition)
  return {
    min: targetCenter - targetSpan * 0.5,
    max: targetCenter + targetSpan * 0.5,
  }
}

test('computeCurtainPanelPose 在关闭状态保持 authored bounds 不变', () => {
  const authoredBounds = { min: 0.6385, max: 1.8840 }
  const pose = computeCurtainPanelPose({
    side: 'left',
    authoredPosition: 1.261,
    authoredBounds,
    authoredScale: 1,
    windowCenter: 0.015,
    openRatio: 0,
    minGatherScale: 0.18,
  })
  const projected = projectBoundsFromPose(authoredBounds, 1.261, 1, pose)

  expect(Math.abs(pose.position - 1.261)).toBeLessThan(1e-9)
  expect(Math.abs(pose.scale - 1)).toBeLessThan(1e-9)
  expect(Math.abs(projected.min - authoredBounds.min)).toBeLessThan(1e-9)
  expect(Math.abs(projected.max - authoredBounds.max)).toBeLessThan(1e-9)
})

test('computeCurtainPanelPose 在打开状态固定左帘外侧边缘，只让内侧边缘向外滑动', () => {
  const authoredBounds = { min: 0.6385, max: 1.8840 }
  const pose = computeCurtainPanelPose({
    side: 'left',
    authoredPosition: 1.261,
    authoredBounds,
    authoredScale: 1,
    windowCenter: 0.015,
    openRatio: 1,
    minGatherScale: 0.18,
  })
  const projected = projectBoundsFromPose(authoredBounds, 1.261, 1, pose)

  expect(Math.abs(pose.scale - 0.18)).toBeLessThan(1e-9)
  expect(Math.abs(projected.max - authoredBounds.max)).toBeLessThan(1e-9)
  assert.ok(projected.min > authoredBounds.min)
})

test('computeCurtainPanelPose 在中间态保持左右对称，右帘外侧边缘不动', () => {
  const authoredBounds = { min: -1.8535, max: -0.6085 }
  const pose = computeCurtainPanelPose({
    side: 'right',
    authoredPosition: -1.231,
    authoredBounds,
    authoredScale: 1,
    windowCenter: 0.015,
    openRatio: 0.5,
    minGatherScale: 0.18,
  })
  const projected = projectBoundsFromPose(authoredBounds, -1.231, 1, pose)

  expect(Math.abs(pose.scale - 0.59)).toBeLessThan(1e-9)
  expect(Math.abs(projected.min - authoredBounds.min)).toBeLessThan(1e-9)
  assert.ok(projected.max < authoredBounds.max)
})

test('computeCameraConeOpacity 只在选中时显示锥形高亮，离线机位仍保留选中反馈', () => {
  assert.equal(computeCameraConeOpacity({ isSelected: false, online: true, pulse: 0.5 }), 0)
  assert.equal(computeCameraConeOpacity({ isSelected: true, online: true, pulse: 0.5 }), 0.86)
  assert.equal(computeCameraConeOpacity({ isSelected: true, online: false, pulse: 0.5 }), 0.28)
})
