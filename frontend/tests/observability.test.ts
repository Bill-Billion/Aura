
import type { AgentState } from '../src/types/world-state.ts'
import type { SimEvent } from '../src/types/sim-event.ts'
import {
  buildEpisodeNodes,
  buildEpisodeSummaries,
  buildEventDetailView,
  categorizeSimEvent,
  compareSimEvents,
  deriveObservabilityState,
  pickDefaultEpisode,
} from '../src/utils/observability.ts'

function makeEvent(overrides: Partial<SimEvent> & Pick<SimEvent, 'event_id' | 'event_type' | 'correlation_id'>): SimEvent {
  return {
    source: 'test_source',
    timestamp: 1,
    wall_time: 1,
    causal_parent: null,
    priority: 1,
    data: {},
    ...overrides,
  } as SimEvent
}

function makeAgents(activeCorrelationId: string | null = null): Record<string, AgentState> {
  return {
    lighting_agent: {
      id: 'lighting_agent',
      name: 'Lighting Agent',
      status: activeCorrelationId ? 'running' : 'idle',
      current_strategy: '',
      confidence: 0.8,
      last_action: '',
      mode: activeCorrelationId ? 'reasoning' : 'idle',
      active_correlation_id: activeCorrelationId,
      last_reasoning_step: '',
      last_fallback_reason: null,
    },
  }
}

test('categorizeSimEvent 会把结构化事件归到六类里', () => {
  assert.equal(
    categorizeSimEvent(makeEvent({ event_id: 'evt-1', event_type: 'reasoning.intent_recognized', correlation_id: 'ep-1' })),
    'reasoning',
  )
  assert.equal(
    categorizeSimEvent(makeEvent({ event_id: 'evt-2', event_type: 'feedback.state_delta', correlation_id: 'ep-1' })),
    'feedback',
  )
})

test('canonical seq 优先于可能回拨的 timestamp 和 wall_time', () => {
  const publishedFirst = makeEvent({
    event_id: 'evt-first',
    event_type: 'reasoning.intent_recognized',
    correlation_id: 'ep-1',
    seq: 10,
    timestamp: 100,
    wall_time: 100,
  })
  const publishedSecond = makeEvent({
    event_id: 'evt-second',
    event_type: 'reasoning.execution_plan',
    correlation_id: 'ep-1',
    seq: 11,
    timestamp: 1,
    wall_time: 1,
  })

  expect(compareSimEvents(publishedFirst, publishedSecond)).toBeLessThan(0)
  expect(buildEpisodeSummaries([publishedSecond, publishedFirst])[0].events.map((event) => event.event_id))
    .toEqual(['evt-first', 'evt-second'])

  const episodes = buildEpisodeSummaries([
    publishedFirst,
    publishedSecond,
    makeEvent({
      event_id: 'evt-newest',
      event_type: 'user.command',
      correlation_id: 'ep-2',
      seq: 12,
      timestamp: 0,
      wall_time: 0,
    }),
  ])
  expect(episodes.map((episode) => episode.correlationId)).toEqual(['ep-2', 'ep-1'])
})

test('buildEpisodeSummaries 会按 correlation_id 分组并优先选择 causal_parent 为空的根事件', () => {
  const events = [
    makeEvent({
      event_id: 'evt-1',
      event_type: 'user.command',
      correlation_id: 'ep-1',
      timestamp: 10,
      wall_time: 10,
      data: {
        message_type: 'CMD_DEVICE_CONTROL',
        device_id: 'light_living_01',
        action: 'turn_on',
        params: {},
      },
    }),
    makeEvent({
      event_id: 'evt-2',
      event_type: 'reasoning.intent_recognized',
      correlation_id: 'ep-1',
      timestamp: 11,
      wall_time: 11,
      causal_parent: 'evt-1',
      data: {
        agent_id: 'lighting_agent',
        intent: 'light occupied room',
        confidence: 0.94,
        explanation: 'Occupancy increased in the living room during the evening',
        provider: 'openai_responses',
        model: 'gpt-5.4',
        latency_ms: 320,
      },
    }),
    makeEvent({
      event_id: 'evt-3',
      event_type: 'reasoning.fallback_rule_based',
      correlation_id: 'ep-2',
      timestamp: 20,
      wall_time: 20,
      causal_parent: 'evt-9',
      data: {
        agent_id: 'lighting_agent',
        reason: 'timeout',
        failed_step: 'intent_generation',
        fallback_strategy: 'rule_based',
      },
    }),
    makeEvent({
      event_id: 'evt-4',
      event_type: 'action.device_control',
      correlation_id: 'ep-2',
      timestamp: 21,
      wall_time: 21,
      causal_parent: 'evt-3',
      data: {
        agent_name: 'lighting_agent',
        device_id: 'light_living_01',
        property: 'state.power',
        value: true,
        reason: 'fallback action',
      },
    }),
  ]

  const summaries = buildEpisodeSummaries(events, makeAgents('ep-2'))

  expect(summaries.length).toBe(2)
  expect(summaries[0].correlationId).toBe('ep-2')
  expect(summaries[0].rootEventId).toBe('evt-3')
  expect(summaries[0].hasFallback).toBe(true)
  expect(summaries[0].isActive).toBe(true)
  expect(summaries[0].primaryAgentId).toBe('lighting_agent')
  expect(summaries[1].rootEventId).toBe('evt-1')
})

test('pickDefaultEpisode 会优先选择最新活跃 episode，否则回退到最近完成的一条', () => {
  const inactive = buildEpisodeSummaries([
    makeEvent({ event_id: 'evt-1', event_type: 'user.activity_change', correlation_id: 'ep-1', wall_time: 10 }),
    makeEvent({ event_id: 'evt-2', event_type: 'user.activity_change', correlation_id: 'ep-2', wall_time: 20 }),
  ])

  expect(pickDefaultEpisode(inactive)?.correlationId).toBe('ep-2')

  const active = buildEpisodeSummaries([
    makeEvent({ event_id: 'evt-3', event_type: 'user.activity_change', correlation_id: 'ep-3', wall_time: 30 }),
    makeEvent({ event_id: 'evt-4', event_type: 'user.activity_change', correlation_id: 'ep-4', wall_time: 40 }),
  ], makeAgents('ep-3'))

  expect(pickDefaultEpisode(active)?.correlationId).toBe('ep-3')
})

test('episode cancellation 只由 system.episode_cancelled 标记，stale decision discard 不是取消', () => {
  const summaries = buildEpisodeSummaries([
    makeEvent({
      event_id: 'cancel-root',
      event_type: 'user.command',
      correlation_id: 'cancelled-episode',
      wall_time: 10,
    }),
    makeEvent({
      event_id: 'cancel-event',
      event_type: 'system.episode_cancelled',
      correlation_id: 'cancelled-episode',
      causal_parent: 'cancel-root',
      wall_time: 11,
      data: { reason: 'simulation_reset' },
    }),
    makeEvent({
      event_id: 'discard-root',
      event_type: 'reasoning.decision_discarded',
      correlation_id: 'discarded-decision',
      wall_time: 20,
      data: { reason: 'stale' },
    }),
  ])

  expect(summaries.find((episode) => episode.correlationId === 'cancelled-episode')?.hasCancelled).toBe(true)
  expect(summaries.find((episode) => episode.correlationId === 'discarded-decision')?.hasCancelled).toBe(false)
})

test('buildEpisodeNodes 会按因果关系展开单条 episode，并给出稳定层级', () => {
  const episode = buildEpisodeSummaries([
    makeEvent({
      event_id: 'evt-root',
      event_type: 'user.command',
      correlation_id: 'ep-1',
      timestamp: 1,
      wall_time: 1,
      data: {
        message_type: 'CMD_DEVICE_CONTROL',
        device_id: 'light_living_01',
        action: 'turn_on',
        params: {},
      },
    }),
    makeEvent({
      event_id: 'evt-intent',
      event_type: 'reasoning.intent_recognized',
      correlation_id: 'ep-1',
      timestamp: 2,
      wall_time: 2,
      causal_parent: 'evt-root',
      data: {
        agent_id: 'lighting_agent',
        intent: 'light occupied room',
        confidence: 0.94,
        explanation: 'Occupancy increased in the living room during the evening',
        provider: 'openai_responses',
        model: 'gpt-5.4',
        latency_ms: 320,
      },
    }),
    makeEvent({
      event_id: 'evt-action',
      event_type: 'action.device_control',
      correlation_id: 'ep-1',
      timestamp: 3,
      wall_time: 3,
      causal_parent: 'evt-intent',
      data: {
        agent_name: 'lighting_agent',
        device_id: 'light_living_01',
        property: 'state.power',
        value: true,
        reason: 'occupied evening lighting',
      },
    }),
    makeEvent({
      event_id: 'evt-feedback',
      event_type: 'feedback.state_delta',
      correlation_id: 'ep-1',
      timestamp: 4,
      wall_time: 4,
      causal_parent: 'evt-action',
      data: {
        path: 'devices[light_living_01].state.power',
        new_value: true,
        caused_by: 'lighting_agent',
        reason: 'sync state',
      },
    }),
  ])[0]

  const nodes = buildEpisodeNodes(episode)

  expect(nodes.map((node) => [node.eventId, node.depth, node.isRoot])).toEqual([
    ['evt-root', 0, true],
    ['evt-intent', 1, false],
    ['evt-action', 2, false],
    ['evt-feedback', 3, false],
  ])
})

test('buildEventDetailView 会把 reasoning 和 feedback 事件映射成固定详情视图', () => {
  const intentDetail = buildEventDetailView(makeEvent({
    event_id: 'evt-intent',
    event_type: 'reasoning.intent_recognized',
    correlation_id: 'ep-1',
    wall_time: 100,
    data: {
      agent_id: 'lighting_agent',
      intent: 'light occupied room',
      confidence: 0.94,
      explanation: 'Occupancy increased in the living room during the evening',
      provider: 'openai_responses',
      model: 'gpt-5.4',
      latency_ms: 320,
    },
  }))

  expect(intentDetail.kind).toBe('reasoning')
  expect(intentDetail.title).toBe('意图识别')
  expect(intentDetail.fields.find((field) => field.label === '意图')?.value).toBe('light occupied room')
  expect(intentDetail.reasoningSteps?.[1]?.state).toBe('current')

  const feedbackDetail = buildEventDetailView(makeEvent({
    event_id: 'evt-feedback',
    event_type: 'feedback.state_delta',
    correlation_id: 'ep-1',
    wall_time: 101,
    data: {
      path: 'devices[light_living_01].state.power',
      new_value: true,
      caused_by: 'lighting_agent',
      reason: 'sync state',
    },
  }))

  expect(feedbackDetail.kind).toBe('feedback')
  expect(feedbackDetail.fields.find((field) => field.label === '路径')?.value).toBe('devices[light_living_01].state.power')
  expect(feedbackDetail.fields.find((field) => field.label === '新值')?.value).toBe('true')
})

// S1 之后 UI 来源的命令也会外发 action.device_control，且后端刻意不伪造 agent 身份
// （backend/execution/command.py::_actor_fields），因此详情面板必须能在无 agent_name 时降级。
test('buildEventDetailView 的 action 详情：有 agent 身份时显示 Agent，UI 来源时降级成命令来源', () => {
  const agentDetail = buildEventDetailView(makeEvent({
    event_id: 'evt-action-agent',
    event_type: 'action.device_control',
    source: 'lighting_agent',
    correlation_id: 'ep-1',
    wall_time: 102,
    data: {
      command_id: 'cmd-1',
      agent_id: 'lighting_agent',
      agent_name: 'Lighting Agent',
      device_id: 'light_living_01',
      capability: 'power',
      property: 'power',
      value: true,
      reason: 'occupied evening lighting',
      source: 'agent',
    },
  }))

  expect(agentDetail.kind).toBe('action')
  expect(agentDetail.fields.find((field) => field.label === '执行 Agent')?.value).toBe('Lighting Agent')
  expect(agentDetail.fields.some((field) => field.label === '执行来源')).toBe(false)

  const uiDetail = buildEventDetailView(makeEvent({
    event_id: 'evt-action-ui',
    event_type: 'action.device_control',
    source: 'command_executor',
    correlation_id: 'ep-2',
    wall_time: 103,
    data: {
      command_id: 'cmd-2',
      device_id: 'light_living_01',
      capability: 'brightness',
      property: 'extra.brightness',
      value: 60,
      reason: '用户直接调节亮度',
      source: 'ui',
    },
  }))

  expect(uiDetail.kind).toBe('action')
  expect(uiDetail.fields.some((field) => field.label === '执行 Agent')).toBe(false)
  expect(uiDetail.fields.find((field) => field.label === '执行来源')?.value).toBe('用户操作')
  // 任何一个字段都不能渲染成空串，否则详情面板会出现一行没有值的占位。
  expect(uiDetail.fields.every((field) => field.value.length > 0)).toBe(true)

  const scenarioDetail = buildEventDetailView(makeEvent({
    event_id: 'evt-action-scenario',
    event_type: 'action.device_control',
    source: 'command_executor',
    correlation_id: 'ep-3',
    wall_time: 104,
    data: {
      device_id: 'curtain_living_01',
      capability: 'open_percent',
      property: 'extra.open_percent',
      value: 80,
      reason: '场景脚本步骤 2',
      source: 'scenario',
    },
  }))

  expect(scenarioDetail.fields.find((field) => field.label === '执行来源')?.value).toBe('场景脚本')

  // 兜底：后端没给 source 时也不能留空字段。
  const unknownDetail = buildEventDetailView(makeEvent({
    event_id: 'evt-action-unknown',
    event_type: 'action.device_control',
    source: 'command_executor',
    correlation_id: 'ep-4',
    wall_time: 105,
    data: {
      device_id: 'fan_bedroom_01',
      property: 'power',
      value: true,
      reason: '未知来源',
    },
  }))

  expect(unknownDetail.fields.find((field) => field.label === '执行来源')?.value).toBe('未知来源')
  expect(unknownDetail.fields.every((field) => field.value.length > 0)).toBe(true)
})

test('deriveObservabilityState 会覆盖 loading、disconnected、needs_start、fallback 四种面板状态', () => {
  const episode = buildEpisodeSummaries([
    makeEvent({
      event_id: 'evt-1',
      event_type: 'reasoning.fallback_rule_based',
      correlation_id: 'ep-1',
      wall_time: 10,
      data: {
        agent_id: 'lighting_agent',
        reason: 'timeout',
        failed_step: 'intent_generation',
        fallback_strategy: 'rule_based',
      },
    }),
  ])[0]

  expect(deriveObservabilityState({
    connectionState: 'connecting',
    isRunning: false,
    selectedEpisode: null,
    selectedEvent: null,
  }).status).toBe('loading')

  expect(deriveObservabilityState({
    connectionState: 'disconnected',
    isRunning: true,
    selectedEpisode: episode,
    selectedEvent: episode.events[0],
  }).status).toBe('disconnected')

  expect(deriveObservabilityState({
    connectionState: 'connected',
    isRunning: false,
    selectedEpisode: null,
    selectedEvent: null,
  }).status).toBe('needs_start')

  const ready = deriveObservabilityState({
    connectionState: 'connected',
    isRunning: true,
    selectedEpisode: episode,
    selectedEvent: null,
  })

  expect(ready.status).toBe('empty')
  expect(ready.fallbackMessage).toBe('LLM 超时，已回退规则链路')
})

test('500 条事件窗口截断后，episode 派生仍然稳定落在最新事件集合上', () => {
  const events = Array.from({ length: 502 }, (_, index) => makeEvent({
    event_id: `evt-${index}`,
    event_type: 'system.timer_tick',
    correlation_id: `ep-${index}`,
    timestamp: index,
    wall_time: index,
    data: {
      tick: index,
      simulated_dt: 1,
      simulation_speed: 1,
    },
  }))

  const summaries = buildEpisodeSummaries(events.slice(-500))

  expect(summaries.length).toBe(500)
  expect(summaries[0].correlationId).toBe('ep-501')
  expect(summaries[summaries.length - 1].correlationId).toBe('ep-2')
})
