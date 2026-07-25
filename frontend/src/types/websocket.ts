import type { SimEvent } from '@/types/sim-event'
import type { AgentState, DeltaChange, WorldStateSnapshot } from '@/types/world-state'

export type CommandMessageType =
  | 'CMD_SIM_START'
  | 'CMD_SIM_PAUSE'
  | 'CMD_SIM_RESET'
  | 'CMD_SIM_SPEED'
  | 'CMD_SIM_MODE'
  | 'CMD_DEVICE_CONTROL'
  | 'CMD_RUN_SCENARIO'
  | 'CMD_SCENE_APPLY'
  | 'CMD_TRIGGER_EVENT'
  | 'HEARTBEAT_PONG'

export type ServerMessageType =
  | 'STATE_FULL'
  | 'STATE_DELTA'
  | 'SIM_EVENT'
  | 'AGENT_STATUS'
  | 'SIMULATION_STATUS'
  | 'ERROR'
  | 'HEARTBEAT_PING'

export type MessageType = ServerMessageType | CommandMessageType | 'EVENT_NOTIFICATION'

export interface WSMessage<TPayload = unknown> {
  type: MessageType | string
  id?: string
  timestamp?: number
  payload: TPayload
}

export interface ErrorPayload {
  code: string
  message: string
  details?: Record<string, unknown> | null
}

export interface AgentStatusPayload {
  agents: Record<string, AgentState>
}

export interface SimulationStatusPayload {
  is_running?: boolean
  speed?: number
  mode?: 'observe' | 'demo'
  wall_tick_ms?: number
  simulated_dt_seconds?: number
}

export interface StateDeltaPayload {
  deltas: DeltaChange[]
}

export type StateFullMessage = WSMessage<WorldStateSnapshot> & { type: 'STATE_FULL' }
export type StateDeltaMessage = WSMessage<StateDeltaPayload> & { type: 'STATE_DELTA' }
export type SimEventMessage = WSMessage<SimEvent> & { type: 'SIM_EVENT' }
export type AgentStatusMessage = WSMessage<AgentStatusPayload> & { type: 'AGENT_STATUS' }
export type SimulationStatusMessage = WSMessage<SimulationStatusPayload> & { type: 'SIMULATION_STATUS' }
export type ErrorWSMessage = WSMessage<ErrorPayload> & { type: 'ERROR' }
export type HeartbeatPingMessage = WSMessage<Record<string, never>> & { type: 'HEARTBEAT_PING' }

export type ServerMessage =
  | StateFullMessage
  | StateDeltaMessage
  | SimEventMessage
  | AgentStatusMessage
  | SimulationStatusMessage
  | ErrorWSMessage
  | HeartbeatPingMessage
