
import type { DeviceCapability, DeviceState, DeviceType } from '../src/types/world-state.ts'
import { hasDeviceCapability, isDeviceWritable } from '../src/utils/deviceFloorMap.ts'

function makeDevice(
  type: DeviceType,
  capabilities: DeviceCapability[],
  extra: Record<string, unknown> = {},
): DeviceState {
  return {
    id: `${type}_test_01`,
    type,
    location: { room: 'living_room', x: 0, y: 0, z: 0 },
    display_name: `${type} 测试设备`,
    floor_id: 'floor_1',
    ui_group: 'device',
    capabilities,
    scene_bindings: {},
    state: { power: false, last_changed_by: 'system', extra },
  }
}

// 能力名与 backend/config/device_registry.py 的注册表条目逐条对齐（S1 之后）。
test('isDeviceWritable 只认可写能力：camera 的 online 与 sensor 的 value 都不算可写', () => {
  assert.equal(isDeviceWritable(makeDevice('camera', ['view', 'online'], { online: true })), false)
  assert.equal(isDeviceWritable(makeDevice('sensor', ['value'], { value: 25.0 })), false)
  assert.equal(isDeviceWritable(makeDevice('light', ['power', 'brightness', 'color_temp'])), true)
  assert.equal(isDeviceWritable(makeDevice('hvac', ['power', 'target_temp', 'mode', 'speed'])), true)
  assert.equal(isDeviceWritable(makeDevice('curtain', ['open_percent'])), true)
  assert.equal(isDeviceWritable(makeDevice('fan', ['power', 'speed', 'shake', 'timeout'])), true)
  assert.equal(isDeviceWritable(null), false)
  assert.equal(isDeviceWritable(makeDevice('sensor', [])), false)
})

test('hasDeviceCapability 按声明的能力位判断，不受可写性影响', () => {
  const camera = makeDevice('camera', ['view', 'online'], { online: true })

  assert.equal(hasDeviceCapability(camera, 'online'), true)
  assert.equal(hasDeviceCapability(camera, 'power'), false)
  assert.equal(hasDeviceCapability(makeDevice('sensor', ['value']), 'value'), true)
  assert.equal(hasDeviceCapability(makeDevice('fan', ['power', 'speed']), 'power'), true)
  assert.equal(hasDeviceCapability(null, 'power'), false)
})
