import type { AgentState } from '../types/world-state'
import type {
  ActionDeviceControlData,
  CommandProposalData,
  ConnectionStateDerived,
  EnvironmentStateRefreshData,
  EventCategory,
  EventDetailCommand,
  EventDetailField,
  EventDetailView,
  EventFilters,
  EpisodeNode,
  EpisodeSummary,
  KnownSimEventType,
  ObservabilityStateView,
  ReasoningCoordinationDecisionData,
  ReasoningExecutionPlanData,
  ReasoningFallbackRuleBasedData,
  ReasoningIntentRecognizedData,
  ReasoningPerceptionSnapshotData,
  ReasoningStepView,
  ReasoningTaskDecompositionData,
  SimEvent,
  SystemSimulationStatusData,
  SystemTimerTickData,
  UserActivityChangeData,
  UserCommandData,
} from '../types/sim-event'

const REASONING_SEQUENCE: KnownSimEventType[] = [
  'reasoning.perception_snapshot',
  'reasoning.intent_recognized',
  'reasoning.task_decomposition',
  'reasoning.coordination_decision',
  'reasoning.execution_plan',
  'reasoning.fallback_rule_based',
]

const EVENT_LABELS: Record<string, string> = {
  'system.timer_tick': '定时推进',
  'system.simulation_started': '模拟已启动',
  'system.simulation_paused': '模拟已暂停',
  'system.simulation_reset': '模拟已重置',
  'environment.state_refresh': '环境刷新',
  'user.command': '用户指令',
  'user.activity_change': '用户活动变化',
  'reasoning.perception_snapshot': '感知快照',
  'reasoning.intent_recognized': '意图识别',
  'reasoning.task_decomposition': '任务拆解',
  'reasoning.coordination_decision': '协调决策',
  'reasoning.execution_plan': '执行计划',
  'reasoning.fallback_rule_based': '规则回退',
  'action.device_control': '设备动作',
  'feedback.state_delta': '状态反馈',
}

const REASONING_STEP_LABELS: Record<string, string> = {
  'reasoning.perception_snapshot': '感知',
  'reasoning.intent_recognized': '意图',
  'reasoning.task_decomposition': '拆解',
  'reasoning.coordination_decision': '协调',
  'reasoning.execution_plan': '执行',
  'reasoning.fallback_rule_based': '回退',
}

export function createDefaultEventFilters(): EventFilters {
  return {
    category: 'all',
    agentId: 'all',
    fallbackOnly: false,
    deviceId: null,
  }
}

export function categorizeSimEvent(event: SimEvent): EventCategory {
  if (event.event_type.startsWith('environment.')) {
    return 'environment'
  }
  if (event.event_type.startsWith('user.')) {
    return 'user'
  }
  if (event.event_type.startsWith('reasoning.')) {
    return 'reasoning'
  }
  if (event.event_type.startsWith('action.')) {
    return 'action'
  }
  if (event.event_type.startsWith('feedback.')) {
    return 'feedback'
  }
  return 'system'
}

export function getEventTypeLabel(eventType: string): string {
  return EVENT_LABELS[eventType] ?? eventType
}

export function summarizeSimEvent(event: SimEvent): string {
  switch (event.event_type) {
    case 'user.command': {
      const data = event.data as UserCommandData
      return `${data.device_id} · ${data.action}`
    }
    case 'user.activity_change': {
      const data = event.data as UserActivityChangeData
      return `${data.user_id} -> ${data.to_room}`
    }
    case 'reasoning.perception_snapshot': {
      const data = event.data as ReasoningPerceptionSnapshotData
      return data.world_summary
    }
    case 'reasoning.intent_recognized': {
      const data = event.data as ReasoningIntentRecognizedData
      return data.intent
    }
    case 'reasoning.task_decomposition': {
      const data = event.data as ReasoningTaskDecompositionData
      return `${data.task_steps.length} steps`
    }
    case 'reasoning.coordination_decision': {
      const data = event.data as ReasoningCoordinationDecisionData
      return `${data.outcome} · ${data.priority}`
    }
    case 'reasoning.execution_plan': {
      const data = event.data as ReasoningExecutionPlanData
      return `${data.execution_mode} · ${data.commands.length} commands`
    }
    case 'reasoning.fallback_rule_based': {
      const data = event.data as ReasoningFallbackRuleBasedData
      return `${data.reason} · ${data.failed_step}`
    }
    case 'action.device_control': {
      const data = event.data as ActionDeviceControlData
      return `${data.device_id}.${data.property} = ${formatInlineValue(data.value)}`
    }
    case 'feedback.state_delta': {
      const data = event.data as Record<string, unknown>
      return `${String(data.path ?? 'delta')} -> ${formatInlineValue(data.new_value)}`
    }
    case 'environment.state_refresh': {
      const data = event.data as EnvironmentStateRefreshData
      return `${data.time_of_day} · ${data.outdoor_temp} C`
    }
    case 'system.timer_tick': {
      const data = event.data as SystemTimerTickData
      return `tick ${data.tick}`
    }
    case 'system.simulation_started':
    case 'system.simulation_paused':
    case 'system.simulation_reset': {
      const data = event.data as SystemSimulationStatusData
      return data.scene_id ? `scene ${data.scene_id}` : 'lifecycle'
    }
    default:
      return event.event_type
  }
}

export function extractEventAgentId(event: SimEvent): string | null {
  const data = event.data as Record<string, unknown>

  if (typeof data.agent_id === 'string' && data.agent_id.length > 0) {
    return data.agent_id
  }
  if (typeof data.agent_name === 'string' && data.agent_name.length > 0) {
    return data.agent_name
  }
  if (typeof data.caused_by === 'string' && data.caused_by.length > 0) {
    return data.caused_by
  }
  if (typeof event.source === 'string' && event.source.endsWith('_agent')) {
    return event.source
  }
  return null
}

export function compareSimEvents(a: SimEvent, b: SimEvent): number {
  if (a.timestamp !== b.timestamp) {
    return a.timestamp - b.timestamp
  }
  if (a.wall_time !== b.wall_time) {
    return a.wall_time - b.wall_time
  }
  return a.event_id.localeCompare(b.event_id)
}

export function buildEpisodeSummaries(
  events: SimEvent[],
  agents: Record<string, AgentState> = {},
): EpisodeSummary[] {
  const groups = new Map<string, SimEvent[]>()

  for (const event of [...events].sort(compareSimEvents)) {
    const current = groups.get(event.correlation_id)
    if (current) {
      current.push(event)
    } else {
      groups.set(event.correlation_id, [event])
    }
  }

  const activeAgentsByCorrelation = new Map<string, string[]>()
  for (const agent of Object.values(agents)) {
    if (!agent.active_correlation_id) {
      continue
    }
    const current = activeAgentsByCorrelation.get(agent.active_correlation_id) ?? []
    current.push(agent.id)
    activeAgentsByCorrelation.set(agent.active_correlation_id, current)
  }

  return Array.from(groups.entries())
    .map(([correlationId, groupedEvents]) => {
      const rootEvent = groupedEvents.find((event) => event.causal_parent === null) ?? groupedEvents[0]
      const lastEvent = groupedEvents[groupedEvents.length - 1]
      const eventAgentIds = uniqueStrings(groupedEvents.map(extractEventAgentId))
      const activeAgents = uniqueStrings(activeAgentsByCorrelation.get(correlationId) ?? [])
      const primaryAgentId = activeAgents[0] ?? eventAgentIds[0] ?? null

      const nonTickCount = groupedEvents.filter((e) => e.event_type !== 'system.timer_tick').length
      const hasCancelled = groupedEvents.some(
        (e) => e.event_type === 'reasoning.decision_discarded' ||
        (e.event_type === 'reasoning.fallback_rule_based' && (e.data as Record<string, unknown>)?.reason === 'discarded'),
      )

      return {
        correlationId,
        rootEventId: rootEvent.event_id,
        rootEventType: rootEvent.event_type,
        rootEvent,
        lastEventId: lastEvent.event_id,
        lastUpdatedAt: lastEvent.wall_time ?? lastEvent.timestamp,
        eventCount: groupedEvents.length,
        nonTickEventCount: nonTickCount,
        primaryAgentId,
        agentIds: uniqueStrings([...activeAgents, ...eventAgentIds]),
        hasFallback: groupedEvents.some((event) => event.event_type === 'reasoning.fallback_rule_based'),
        hasCancelled,
        isActive: activeAgents.length > 0,
        categories: uniqueStrings(groupedEvents.map((event) => categorizeSimEvent(event))) as EventCategory[],
        events: groupedEvents,
      }
    })
    .sort((left, right) => {
      if (left.lastUpdatedAt !== right.lastUpdatedAt) {
        return right.lastUpdatedAt - left.lastUpdatedAt
      }
      return compareSimEvents(right.rootEvent, left.rootEvent)
    })
}

export function filterEpisodes(
  episodes: EpisodeSummary[],
  filters: EventFilters,
): EpisodeSummary[] {
  return episodes.filter((episode) => {
    if (filters.category !== 'all' && !episode.categories.includes(filters.category)) {
      return false
    }
    if (filters.agentId !== 'all' && !episode.agentIds.includes(filters.agentId)) {
      return false
    }
    if (filters.fallbackOnly && !episode.hasFallback) {
      return false
    }
    if (filters.deviceId) {
      // 检查 episode 中是否有事件引用了该设备
      const hasDeviceEvent = episode.events.some((event) => {
        const data = event.data as Record<string, unknown>
        return data?.device_id === filters.deviceId ||
          data?.device_ids && Array.isArray(data.device_ids) && data.device_ids.includes(filters.deviceId)
      })
      if (!hasDeviceEvent) return false
    }
    return true
  })
}

export function pickDefaultEpisode(episodes: EpisodeSummary[]): EpisodeSummary | null {
  return episodes.find((episode) => episode.isActive) ?? episodes[0] ?? null
}

export function buildEpisodeNodes(episode: EpisodeSummary): EpisodeNode[] {
  const orderedEvents = [...episode.events].sort(compareSimEvents)
  const eventMap = new Map(orderedEvents.map((event) => [event.event_id, event]))
  const children = new Map<string, SimEvent[]>()

  for (const event of orderedEvents) {
    if (!event.causal_parent || !eventMap.has(event.causal_parent) || event.causal_parent === event.event_id) {
      continue
    }
    const current = children.get(event.causal_parent) ?? []
    current.push(event)
    children.set(event.causal_parent, current)
  }

  for (const entries of children.values()) {
    entries.sort(compareSimEvents)
  }

  const nodes: EpisodeNode[] = []
  const visited = new Set<string>()
  const rootEvent = eventMap.get(episode.rootEventId) ?? orderedEvents[0]

  const appendTree = (event: SimEvent, depth: number, isOrphanRoot: boolean) => {
    if (visited.has(event.event_id)) {
      return
    }
    visited.add(event.event_id)
    const parentEventId =
      depth === 0 || isOrphanRoot || !event.causal_parent || !eventMap.has(event.causal_parent)
        ? null
        : event.causal_parent

    nodes.push({
      eventId: event.event_id,
      parentEventId,
      depth,
      category: categorizeSimEvent(event),
      isRoot: event.event_id === episode.rootEventId,
      isOrphan: isOrphanRoot,
      event,
    })

    for (const child of children.get(event.event_id) ?? []) {
      appendTree(child, depth + 1, false)
    }
  }

  appendTree(rootEvent, 0, false)

  for (const event of orderedEvents) {
    if (!visited.has(event.event_id)) {
      appendTree(event, 1, true)
    }
  }

  return nodes
}

export function buildEventDetailView(event: SimEvent): EventDetailView {
  const category = categorizeSimEvent(event)
  const base = {
    category,
    title: getEventTypeLabel(event.event_type),
    subtitle: formatEventWallTime(event.wall_time),
  }

  switch (event.event_type) {
    case 'reasoning.perception_snapshot': {
      const data = event.data as ReasoningPerceptionSnapshotData
      return {
        ...base,
        kind: 'reasoning',
        summary: data.world_summary,
        fields: [
          createField('执行 Agent', data.agent_id, 'accent'),
          createField('触发事件', data.trigger_event_type),
        ],
        reasoningSteps: buildReasoningSteps(event.event_type),
        listTitle: '关联对象',
        listItems: [
          ...data.relevant_rooms.map((room) => `Room · ${room}`),
          ...data.relevant_devices.map((device) => `Device · ${device}`),
        ],
      }
    }
    case 'reasoning.intent_recognized': {
      const data = event.data as ReasoningIntentRecognizedData
      return {
        ...base,
        kind: 'reasoning',
        summary: data.explanation,
        fields: [
          createField('执行 Agent', data.agent_id, 'accent'),
          createField('意图', data.intent),
          createField('置信度', `${Math.round(data.confidence * 100)}%`),
          createField('推理服务', data.provider),
          createField('模型', data.model),
          createField('耗时', `${data.latency_ms} ms`),
        ],
        reasoningSteps: buildReasoningSteps(event.event_type),
      }
    }
    case 'reasoning.task_decomposition': {
      const data = event.data as ReasoningTaskDecompositionData
      return {
        ...base,
        kind: 'reasoning',
        summary: `围绕 "${data.intent}" 拆成 ${data.task_steps.length} 步`,
        fields: [
          createField('执行 Agent', data.agent_id, 'accent'),
          createField('意图', data.intent),
        ],
        reasoningSteps: buildReasoningSteps(event.event_type),
        listTitle: '任务步骤',
        listItems: data.task_steps,
      }
    }
    case 'reasoning.coordination_decision': {
      const data = event.data as ReasoningCoordinationDecisionData
      return {
        ...base,
        kind: 'reasoning',
        summary: `协调结果：${data.outcome}`,
        fields: [
          createField('执行 Agent', data.agent_id, 'accent'),
          createField('结果', data.outcome),
          createField('优先级', data.priority),
          createField('冲突数', `${data.conflicts.length}`),
        ],
        reasoningSteps: buildReasoningSteps(event.event_type),
        commands: data.winning_commands.map(mapCommandView),
      }
    }
    case 'reasoning.execution_plan': {
      const data = event.data as ReasoningExecutionPlanData
      return {
        ...base,
        kind: 'reasoning',
        summary: `执行模式：${data.execution_mode}`,
        fields: [
          createField('执行 Agent', data.agent_id, 'accent'),
          createField('执行模式', data.execution_mode),
          createField('命令数', `${data.commands.length}`),
        ],
        reasoningSteps: buildReasoningSteps(event.event_type),
        commands: data.commands.map(mapCommandView),
      }
    }
    case 'reasoning.fallback_rule_based': {
      const data = event.data as ReasoningFallbackRuleBasedData
      return {
        ...base,
        kind: 'reasoning',
        summary: 'LLM 超时，已回退规则链路',
        fields: [
          createField('执行 Agent', data.agent_id, 'accent'),
          createField('失败原因', data.reason, 'warning'),
          createField('失败步骤', data.failed_step),
          createField('回退策略', data.fallback_strategy),
        ],
        reasoningSteps: buildReasoningSteps(event.event_type),
      }
    }
    case 'user.command': {
      const data = event.data as UserCommandData
      return {
        ...base,
        kind: 'user',
        summary: `用户直接下发 ${data.action}`,
        fields: [
          createField('消息类型', data.message_type),
          createField('设备', data.device_id, 'accent'),
          createField('动作', data.action),
          createField('参数', formatInlineValue(data.params)),
        ],
      }
    }
    case 'user.activity_change': {
      const data = event.data as UserActivityChangeData
      return {
        ...base,
        kind: 'user',
        summary: `${data.user_id} 进入 ${data.to_room}`,
        fields: [
          createField('用户', data.user_id, 'accent'),
          createField('起点', data.from_room ?? '-'),
          createField('终点', data.to_room),
          createField('活动', data.activity),
        ],
      }
    }
    case 'action.device_control': {
      const data = event.data as ActionDeviceControlData
      return {
        ...base,
        kind: 'action',
        summary: data.reason,
        fields: [
          createActionActorField(data),
          createField('设备', data.device_id),
          createField('属性', data.property),
          createField('目标值', formatInlineValue(data.value)),
          createField('原因', data.reason),
        ],
      }
    }
    case 'feedback.state_delta': {
      const data = event.data as Record<string, unknown>
      return {
        ...base,
        kind: 'feedback',
        summary: typeof data.reason === 'string' ? data.reason : '状态已经同步回世界快照',
        fields: [
          createField('路径', String(data.path ?? '-'), 'accent'),
          createField('新值', formatInlineValue(data.new_value)),
          createField('来源', formatInlineValue(data.caused_by ?? '-')),
          createField('原因', formatInlineValue(data.reason ?? '-')),
        ],
      }
    }
    case 'environment.state_refresh': {
      const data = event.data as EnvironmentStateRefreshData
      return {
        ...base,
        kind: 'environment',
        summary: `${data.time_of_day} · ${data.outdoor_temp} C`,
        fields: [
          createField('模拟步长', `${data.simulated_dt}`),
          createField('时间段', data.time_of_day, 'accent'),
          createField('室外温度', `${data.outdoor_temp} C`),
          createField(
            '变化原因',
            data.significant_change_reasons?.length
              ? data.significant_change_reasons.join(', ')
              : '-',
          ),
        ],
      }
    }
    case 'system.timer_tick': {
      const data = event.data as SystemTimerTickData
      return {
        ...base,
        kind: 'system',
        summary: `tick ${data.tick}`,
        fields: [
          createField('Tick', `${data.tick}`, 'accent'),
          createField('模拟步长', `${data.simulated_dt}`),
          createField('速度', `${data.simulation_speed}x`),
        ],
      }
    }
    case 'system.simulation_started':
    case 'system.simulation_paused':
    case 'system.simulation_reset': {
      const data = event.data as SystemSimulationStatusData
      return {
        ...base,
        kind: 'system',
        summary: '仿真生命周期更新',
        fields: [
          createField('速度', data.simulation_speed ? `${data.simulation_speed}x` : '-'),
          createField('场景', data.scene_id ?? '-', 'accent'),
        ],
      }
    }
    default:
      return {
        ...base,
        kind: 'system',
        summary: summarizeSimEvent(event),
        fields: [createField('Source', event.source)],
      }
  }
}

export function deriveObservabilityState(options: {
  connectionState: ConnectionStateDerived
  isRunning: boolean
  selectedEpisode: EpisodeSummary | null
  selectedEvent: SimEvent | null
}): ObservabilityStateView {
  const { connectionState, isRunning, selectedEpisode, selectedEvent } = options

  if (connectionState === 'connecting') {
    return {
      status: 'loading',
      title: '正在连接事件流',
      message: '正在等待 WebSocket 同步当前仿真状态和 episode 链路。',
    }
  }

  if (connectionState === 'disconnected') {
    return {
      status: 'disconnected',
      title: '连接已断开',
      message: '观测流暂时不可用，重新连接后会继续接收新的 episode。',
    }
  }

  if (!isRunning) {
    return {
      status: 'needs_start',
      title: '尚未启动模拟',
      message: '点击开始模拟，生成新的事件链路。',
    }
  }

  if (!selectedEpisode) {
    return {
      status: 'empty',
      title: '暂无可观测 episode',
      message: '等待新的 root -> reasoning -> action -> feedback 链路进入侧栏。',
    }
  }

  if (!selectedEvent) {
    return {
      status: 'empty',
      title: '选择事件查看详情',
      message: '当前 episode 已经载入，点击时间线节点查看推理和执行细节。',
      fallbackMessage: selectedEpisode.hasFallback ? 'LLM 超时，已回退规则链路' : null,
    }
  }

  return {
    status: 'ready',
    title: '',
    message: '',
    fallbackMessage: selectedEpisode.hasFallback ? 'LLM 超时，已回退规则链路' : null,
  }
}

export function formatEventWallTime(wallTime: number): string {
  return new Date(wallTime).toLocaleTimeString('zh-CN', {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  })
}

function buildReasoningSteps(currentType: string): ReasoningStepView[] {
  const currentIndex = REASONING_SEQUENCE.indexOf(currentType as KnownSimEventType)

  return REASONING_SEQUENCE.map((eventType, index) => ({
    eventType,
    label: REASONING_STEP_LABELS[eventType] ?? getEventTypeLabel(eventType),
    state: index < currentIndex ? 'done' : index === currentIndex ? 'current' : 'pending',
  }))
}

function createField(label: string, value: string, tone: EventDetailField['tone'] = 'default'): EventDetailField {
  return { label, value, tone }
}

const COMMAND_SOURCE_LABELS: Record<string, string> = {
  ui: '用户操作',
  agent: 'Agent',
  scenario: '场景脚本',
  rule_fallback: '规则降级',
}

// S1 之后 UI / 场景脚本来源的命令也会外发 action.device_control，而后端对这些来源
// 刻意不写 agent_id / agent_name（backend/execution/command.py::_actor_fields），
// 所以这里不能无条件渲染"执行 Agent"，否则详情面板会出现一行空值。
function createActionActorField(data: ActionDeviceControlData): EventDetailField {
  if (typeof data.agent_name === 'string' && data.agent_name.length > 0) {
    return createField('执行 Agent', data.agent_name, 'accent')
  }
  if (typeof data.agent_id === 'string' && data.agent_id.length > 0) {
    return createField('执行 Agent', data.agent_id, 'accent')
  }
  const source = typeof data.source === 'string' ? data.source : ''
  // 空串必须兜住：createField 不做 falsy 过滤，ReasoningDetail.vue 也不带 v-if。
  return createField('执行来源', COMMAND_SOURCE_LABELS[source] || source || '未知来源', 'accent')
}

function mapCommandView(command: CommandProposalData): EventDetailCommand {
  return {
    deviceId: command.device_id,
    property: command.property,
    value: formatInlineValue(command.value),
    reason: command.reason,
  }
}

function formatInlineValue(value: unknown): string {
  if (value === null || value === undefined) {
    return '-'
  }
  if (typeof value === 'string') {
    return value
  }
  if (typeof value === 'number' || typeof value === 'boolean') {
    return String(value)
  }
  try {
    return JSON.stringify(value)
  } catch {
    return String(value)
  }
}

function uniqueStrings(values: Array<string | null | undefined>): string[] {
  return Array.from(new Set(values.filter((value): value is string => Boolean(value))))
}
