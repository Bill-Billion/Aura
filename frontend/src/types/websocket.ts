export interface WSMessage<T = any> {
  type: string
  id?: string
  timestamp?: number
  payload: T
}

export type MessageType =
  | 'STATE_FULL' | 'STATE_DELTA' | 'EVENT_NOTIFICATION' | 'SIM_EVENT'
  | 'AGENT_STATUS' | 'SIMULATION_STATUS' | 'HEARTBEAT_PING'
  | 'CMD_SIM_START' | 'CMD_SIM_PAUSE' | 'CMD_SIM_RESET'
  | 'CMD_SIM_SPEED' | 'CMD_DEVICE_CONTROL' | 'CMD_TRIGGER_EVENT'
