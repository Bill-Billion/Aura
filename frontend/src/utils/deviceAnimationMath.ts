export type BindingAxis = 'x' | 'y' | 'z'

export interface AxisBounds {
  min: number
  max: number
}

export interface CurtainTravelInput {
  side: 'left' | 'right'
  panelBounds: AxisBounds | null
  trackBounds: AxisBounds | null
  fallbackSpan: number
}

export interface CurtainPanelPoseInput {
  side: 'left' | 'right'
  authoredPosition: number
  authoredBounds: AxisBounds
  authoredScale: number
  windowCenter: number
  openRatio: number
  minGatherScale: number
}

export interface CurtainPanelPose {
  position: number
  scale: number
}

export interface CameraConeOpacityInput {
  isSelected: boolean
  online: boolean
  pulse: number
}

export function readBoundsAxis(bounds: { min: Record<BindingAxis, number>, max: Record<BindingAxis, number> }, axis: BindingAxis): AxisBounds {
  return {
    min: bounds.min[axis],
    max: bounds.max[axis],
  }
}

/**
 * gamemcu 的布帘是“外侧边缘固定、内侧边缘滑动”。
 * 这里直接在 authored bounds 上做收褶，确保打开时布帘会贴回窗口两侧。
 */
export function computeCurtainPanelPose({
  side,
  authoredPosition,
  authoredBounds,
  authoredScale,
  windowCenter,
  openRatio,
  minGatherScale,
}: CurtainPanelPoseInput): CurtainPanelPose {
  const safeRatio = Math.max(0, Math.min(1, openRatio))
  const authoredSpan = Math.max(authoredBounds.max - authoredBounds.min, 0.0001)
  const authoredCenter = (authoredBounds.min + authoredBounds.max) * 0.5
  const targetSpan = authoredSpan * (1 - (1 - minGatherScale) * safeRatio)

  const minDistance = Math.abs(authoredBounds.min - windowCenter)
  const maxDistance = Math.abs(authoredBounds.max - windowCenter)
  const anchorPositiveEdge = maxDistance === minDistance
    ? side === 'left'
    : maxDistance > minDistance

  const targetBounds = anchorPositiveEdge
    ? {
        min: authoredBounds.max - targetSpan,
        max: authoredBounds.max,
      }
    : {
        min: authoredBounds.min,
        max: authoredBounds.min + targetSpan,
      }

  const targetCenter = (targetBounds.min + targetBounds.max) * 0.5
  const positionOffset = targetCenter - authoredCenter

  return {
    position: authoredPosition + positionOffset,
    scale: authoredScale * (targetSpan / authoredSpan),
  }
}

/**
 * 参考 gamemcu 的 camera controller：锥形高亮只跟当前选中机位走，
 * 离线时仍保留一个更轻的选中反馈，避免看起来像没绑定成功。
 */
export function computeCameraConeOpacity({
  isSelected,
  online,
  pulse,
}: CameraConeOpacityInput): number {
  if (!isSelected) return 0
  if (!online) return 0.28
  return 0.78 + pulse * 0.16
}
