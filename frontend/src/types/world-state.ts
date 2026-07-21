export interface Location3D { room: string; x: number; y: number; z: number }

export type DeviceType = 'light' | 'hvac' | 'curtain' | 'sensor' | 'fan' | 'camera'
export type DeviceUIGroup = 'lighting' | 'device' | 'security' | 'environment'
// 与 backend/execution/capability_matrix.py 的 CAPABILITY_MATRIX 一一对应（spec §3.2）。
// 前九条可写，后三条（view / online / value）只读——只读集合的权威在
// capability_matrix.read_only_capability_names()，前端镜像见 deviceFloorMap.ts。
export type DeviceCapability =
  | 'power'
  | 'brightness'
  | 'color_temp'
  | 'target_temp'
  | 'mode'
  | 'speed'
  | 'open_percent'
  | 'shake'
  | 'timeout'
  | 'view'
  | 'online'
  | 'value'

export interface DeviceState {
  id: string; type: DeviceType; location: Location3D
  display_name: string
  floor_id: string
  ui_group: DeviceUIGroup
  capabilities: DeviceCapability[]
  scene_bindings: Record<string, unknown>
  state: { power: boolean; last_changed_by: string; extra: Record<string, any> }
}

export interface RoomState {
  id: string; temperature: number; humidity: number
  light_level: number; occupancy: boolean; persons: string[]
}

export interface EnvironmentState {
  time_of_day: string; outdoor_temp: number
  outdoor_humidity: number; weather: string
}

export interface AgentState {
  id: string; name: string; status: string
  current_strategy: string; confidence: number; last_action: string
  mode: string
  active_correlation_id: string | null
  last_reasoning_step: string
  last_fallback_reason: string | null
  provider: string
  provider_configured: boolean
  last_latency_ms: number | null
  last_trigger_event: string
}

export interface UserState {
  id: string
  name: string
  location: Location3D | null
  activity: string
  comfort_score: number
}

export interface WorldStateSnapshot {
  simulation_tick: number; simulation_speed: number
  simulation_mode: 'observe' | 'demo'
  wall_tick_ms: number
  simulated_dt_seconds: number
  is_running: boolean; scene_id: string
  environment: EnvironmentState
  devices: Record<string, DeviceState>
  rooms: Record<string, RoomState>
  agents: Record<string, AgentState>
  users: Record<string, UserState>
}

export interface DeltaChange {
  path: string; old_value?: any; new_value: any
  caused_by?: string; reason?: string
}
