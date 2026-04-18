export const DEVICE_FLOOR_OVERRIDES: Record<string, string> = {
  light_living_01: 'F1',
  light_bedroom_01: 'F2',
  curtain_living_01: 'F1',
  ac_living_01: 'F1',
}

export const ROOM_FLOOR_MAP: Record<string, string> = {
  living_room: 'F1',
  kitchen: 'F1',
  bedroom: 'F2',
  bathroom: 'F2',
  loft: 'F3',
  utility: 'F3',
}

/**
 * 统一设备所在楼层的推导逻辑，避免 3D 动画、面板和事件链各自写一套规则。
 */
export function getFloorForDevice(deviceId: string, roomId?: string | null): string | null {
  if (DEVICE_FLOOR_OVERRIDES[deviceId]) {
    return DEVICE_FLOOR_OVERRIDES[deviceId]
  }

  if (roomId && ROOM_FLOOR_MAP[roomId]) {
    return ROOM_FLOOR_MAP[roomId]
  }

  const floorPrefix = deviceId.match(/^L(\d)_/i)
  if (floorPrefix) {
    return `F${floorPrefix[1]}`
  }

  const floorInName = deviceId.match(/(?:^|_)(f[1-3])(?:_|$)/i)
  if (floorInName) {
    return floorInName[1].toUpperCase()
  }

  return null
}
