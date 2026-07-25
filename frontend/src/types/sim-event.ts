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
  | 'reasoning.perception_snapshot'
  | 'reasoning.intent_recognized'
  | 'reasoning.task_decomposition'
  | 'reasoning.coordination_decision'
  | 'reasoning.execution_plan'
  | 'reasoning.fallback_rule_based'
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
  significant_change_reasons?: string[]
  updates?: Record<string, number>
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

// 命令四来源（spec §10 / ws-protocol.md「S1 命令生命周期 payload」）。
export type CommandSource = 'ui' | 'agent' | 'scenario' | 'rule_fallback'

export interface ActionDeviceControlData {
  // agent_id / agent_name 只在有具体执行者时出现：backend/execution/command.py::_actor_fields
  // 对 ui / scenario 来源刻意不伪造 agent 身份，所以这两个字段是可选的。
  agent_id?: string
  agent_name?: string
  command_id?: string
  device_id: string
  capability?: string
  property: string
  value: unknown
  reason: string
  source?: CommandSource | string
}

export interface CommandProposalData {
  device_id: string
  property: string
  value: unknown
  reason: string
}

export interface ReasoningPerceptionSnapshotData {
  agent_id: string
  trigger_event_type: string
  world_summary: string
  relevant_devices: string[]
  relevant_rooms: string[]
}

export interface ReasoningIntentRecognizedData {
  agent_id: string
  intent: string
  confidence: number
  explanation: string
  provider: string
  model: string
  latency_ms: number
}

export interface ReasoningTaskDecompositionData {
  agent_id: string
  intent: string
  task_steps: string[]
}

export interface ReasoningCoordinationDecisionData {
  agent_id: string
  outcome: string
  priority: string
  conflicts: Array<Record<string, unknown>>
  winning_commands: CommandProposalData[]
}

export interface ReasoningExecutionPlanData {
  agent_id: string
  execution_mode: string
  commands: CommandProposalData[]
}

export interface ReasoningFallbackRuleBasedData {
  agent_id: string
  reason: 'timeout' | 'provider_error' | 'invalid_output' | string
  failed_step: string
  fallback_strategy: string
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

export interface ReasoningPerceptionSnapshotEvent
  extends SimEventBase<'reasoning.perception_snapshot', ReasoningPerceptionSnapshotData> {}

export interface ReasoningIntentRecognizedEvent
  extends SimEventBase<'reasoning.intent_recognized', ReasoningIntentRecognizedData> {}

export interface ReasoningTaskDecompositionEvent
  extends SimEventBase<'reasoning.task_decomposition', ReasoningTaskDecompositionData> {}

export interface ReasoningCoordinationDecisionEvent
  extends SimEventBase<'reasoning.coordination_decision', ReasoningCoordinationDecisionData> {}

export interface ReasoningExecutionPlanEvent
  extends SimEventBase<'reasoning.execution_plan', ReasoningExecutionPlanData> {}

export interface ReasoningFallbackRuleBasedEvent
  extends SimEventBase<'reasoning.fallback_rule_based', ReasoningFallbackRuleBasedData> {}

export type KnownSimEvent =
  | SystemTimerTickEvent
  | SystemSimulationStartedEvent
  | SystemSimulationPausedEvent
  | SystemSimulationResetEvent
  | EnvironmentStateRefreshEvent
  | UserCommandEvent
  | UserActivityChangeEvent
  | ReasoningPerceptionSnapshotEvent
  | ReasoningIntentRecognizedEvent
  | ReasoningTaskDecompositionEvent
  | ReasoningCoordinationDecisionEvent
  | ReasoningExecutionPlanEvent
  | ReasoningFallbackRuleBasedEvent
  | ActionDeviceControlEvent
  | FeedbackStateDeltaEvent

export type SimEvent = KnownSimEvent | SimEventBase

export type EventCategory =
  | 'system'
  | 'environment'
  | 'user'
  | 'reasoning'
  | 'action'
  | 'feedback'

export type EventCategoryFilter = EventCategory | 'all'
export type EventAgentFilter = string | 'all'

export interface EventFilters {
  category: EventCategoryFilter
  agentId: EventAgentFilter
  fallbackOnly: boolean
  /** 按设备 ID 过滤——点击 3D 设备时置入，清空即取消过滤 */
  deviceId: string | null
}

export interface EpisodeSummary {
  correlationId: string
  rootEventId: string
  rootEventType: string
  rootEvent: SimEvent
  lastEventId: string
  lastUpdatedAt: number
  eventCount: number
  /** 不含 timer_tick 的有效事件数 */
  nonTickEventCount: number
  primaryAgentId: string | null
  agentIds: string[]
  hasFallback: boolean
  /** episode 被 reasoning.decision_discarded 显式取消 */
  hasCancelled: boolean
  isActive: boolean
  categories: EventCategory[]
  events: SimEvent[]
}

export interface EpisodeNode {
  eventId: string
  parentEventId: string | null
  depth: number
  category: EventCategory
  isRoot: boolean
  isOrphan: boolean
  event: SimEvent
}

export type DetailTone = 'default' | 'muted' | 'accent' | 'warning'

export interface EventDetailField {
  label: string
  value: string
  tone?: DetailTone
}

export interface EventDetailCommand {
  deviceId: string
  property: string
  value: string
  reason: string
}

export interface ReasoningStepView {
  eventType: KnownSimEventType
  label: string
  state: 'done' | 'current' | 'pending'
}

export type EventDetailKind =
  | 'reasoning'
  | 'user'
  | 'action'
  | 'feedback'
  | 'environment'
  | 'system'

export interface EventDetailView {
  kind: EventDetailKind
  category: EventCategory
  title: string
  subtitle: string
  summary?: string
  fields: EventDetailField[]
  reasoningSteps?: ReasoningStepView[]
  listTitle?: string
  listItems?: string[]
  commands?: EventDetailCommand[]
}

export type ObservabilitySurfaceState =
  | 'loading'
  | 'needs_start'
  | 'disconnected'
  | 'empty'
  | 'ready'

export interface ObservabilityStateView {
  status: ObservabilitySurfaceState
  title: string
  message: string
  fallbackMessage?: string | null
}

export type ConnectionStateDerived = 'connecting' | 'connected' | 'disconnected'
