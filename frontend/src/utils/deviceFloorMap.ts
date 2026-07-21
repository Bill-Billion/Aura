import type { DeviceCapability, DeviceState } from '@/types/world-state'

export const ROOM_FLOOR_MAP: Record<string, string> = {
  living_room: 'F1',
  kitchen: 'F1',
  bedroom: 'F2',
  bathroom: 'F2',
  loft: 'F3',
  utility: 'F3',
}

const ROOM_LABELS: Record<string, string> = {
  living_room: '一层客厅',
  kitchen: '一层厨房',
  bedroom: '二层卧室',
  bathroom: '二层卫浴',
  loft: '三层阁楼',
  utility: '三层设备间',
}

const TYPE_LABELS: Record<string, string> = {
  light: '灯光控制',
  hvac: '空调控制',
  curtain: '窗帘控制',
  fan: '风扇控制',
  camera: '摄像头预览',
  sensor: '环境读数',
}

const GROUP_LABELS: Record<string, string> = {
  lighting: '照明',
  device: '设备',
  security: '安防',
  environment: '环境',
}

export function getFloorForDevice(
  deviceId: string,
  roomId?: string | null,
  explicitFloorId?: string | null,
): string | null {
  if (explicitFloorId) {
    return explicitFloorId
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

export function getDeviceLabel(device: DeviceState, fallbackId?: string): string {
  return device.display_name || fallbackId || device.id
}

export function getDeviceTypeLabel(deviceType: string): string {
  return TYPE_LABELS[deviceType] ?? '设备控制'
}

export function getDeviceGroupLabel(group: string): string {
  return GROUP_LABELS[group] ?? '设备'
}

export function getRoomLabel(roomId?: string | null): string {
  if (!roomId) return '场景设备'
  return ROOM_LABELS[roomId] ?? roomId
}

export function hasDeviceCapability(
  device: DeviceState | null | undefined,
  capability: DeviceCapability,
): boolean {
  if (!device) return false
  return device.capabilities.includes(capability)
}

// 白名单而非黑名单：后端新增能力时默认按"不可写"处理，宁可少给一个控件，
// 也不能把只读设备（camera 的 view/online、sensor 的 value）渲染成可操控。
// 权威来源是 backend/execution/capability_matrix.py 里 writable=True 的那几行。
const WRITABLE_DEVICE_CAPABILITIES: ReadonlySet<DeviceCapability> = new Set<DeviceCapability>([
  'power',
  'brightness',
  'color_temp',
  'target_temp',
  'mode',
  'speed',
  'open_percent',
  'shake',
  'timeout',
])

export function isDeviceWritable(device: DeviceState | null | undefined): boolean {
  if (!device) return false
  return device.capabilities.some((capability) => WRITABLE_DEVICE_CAPABILITIES.has(capability))
}

export function isDeviceOnline(device: DeviceState | null | undefined): boolean {
  if (!device) return false
  if (device.type === 'camera') {
    return Boolean(device.state.extra.online)
  }
  if (device.type === 'sensor') {
    return device.state.power
  }
  return Boolean(device.state.power)
}
