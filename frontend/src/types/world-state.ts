export interface Location3D { room: string; x: number; y: number; z: number }

export type DeviceType = 'light' | 'hvac' | 'curtain' | 'sensor' | 'fan' | 'camera'
export type DeviceUIGroup = 'lighting' | 'device' | 'security' | 'environment'
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
  | 'read'

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
