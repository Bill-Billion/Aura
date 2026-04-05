export interface Location3D { room: string; x: number; y: number; z: number }

export interface DeviceState {
  id: string; type: string; location: Location3D
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

export interface WorldStateSnapshot {
  simulation_tick: number; simulation_speed: number
  is_running: boolean; scene_id: string
  environment: EnvironmentState
  devices: Record<string, DeviceState>
  rooms: Record<string, RoomState>
  agents: Record<string, AgentState>
}

export interface DeltaChange {
  path: string; old_value?: any; new_value: any
  caused_by?: string; reason?: string
}
