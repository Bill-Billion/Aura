import type { DeltaChange } from '@/types/world-state'

export type SimEventPriority = 0 | 1 | 2 | 3

export type KnownSimEventType =
  | 'system.timer_tick'
  | 'system.simulation_started'
  | 'system.simulation_paused'
  | 'system.simulation_reset'
  | 'environment.state_refresh'
  | 'user.command'
  | 'user.activity_change'
  | 'action.device_control'
  | 'feedback.state_delta'

export interface SimEventBase<
  TType extends string = string,
  TData = Record<string, unknown>,
> {
  event_id: string
  event_type: TType
  source: string
  timestamp: number
  wall_time: number
  correlation_id: string
  causal_parent: string | null
  priority: SimEventPriority
  data: TData
}

export interface SystemTimerTickData {
  tick: number
  simulated_dt: number
  simulation_speed: number
}

export interface SystemSimulationStatusData {
  simulation_speed?: number
  scene_id?: string
}

export interface EnvironmentStateRefreshData {
  simulated_dt: number
  time_of_day: string
  outdoor_temp: number
}

export interface UserCommandData {
  message_type: string
  device_id: string
  action: string
  params: Record<string, unknown>
}

export interface UserActivityChangeData {
  user_id: string
  from_room?: string
  to_room: string
  activity: string
}

export interface ActionDeviceControlData {
  agent_name: string
  device_id: string
  property: string
  value: unknown
  reason: string
}

export interface SystemTimerTickEvent
  extends SimEventBase<'system.timer_tick', SystemTimerTickData> {}

export interface SystemSimulationStartedEvent
  extends SimEventBase<'system.simulation_started', SystemSimulationStatusData> {}

export interface SystemSimulationPausedEvent
  extends SimEventBase<'system.simulation_paused', SystemSimulationStatusData> {}

export interface SystemSimulationResetEvent
  extends SimEventBase<'system.simulation_reset', SystemSimulationStatusData> {}

export interface EnvironmentStateRefreshEvent
  extends SimEventBase<'environment.state_refresh', EnvironmentStateRefreshData> {}

export interface UserCommandEvent
  extends SimEventBase<'user.command', UserCommandData> {}

export interface UserActivityChangeEvent
  extends SimEventBase<'user.activity_change', UserActivityChangeData> {}

export interface ActionDeviceControlEvent
  extends SimEventBase<'action.device_control', ActionDeviceControlData> {}

export interface FeedbackStateDeltaEvent
  extends SimEventBase<'feedback.state_delta', DeltaChange> {}

export type KnownSimEvent =
  | SystemTimerTickEvent
  | SystemSimulationStartedEvent
  | SystemSimulationPausedEvent
  | SystemSimulationResetEvent
  | EnvironmentStateRefreshEvent
  | UserCommandEvent
  | UserActivityChangeEvent
  | ActionDeviceControlEvent
  | FeedbackStateDeltaEvent

export type SimEvent = KnownSimEvent | SimEventBase
