import test from 'node:test'
import assert from 'node:assert/strict'

import type { AgentState } from '../src/types/world-state.ts'
import type { SimEvent } from '../src/types/sim-event.ts'
import {
  buildEpisodeNodes,
  buildEpisodeSummaries,
  buildEventDetailView,
  categorizeSimEvent,
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

  assert.equal(summaries.length, 2)
  assert.equal(summaries[0].correlationId, 'ep-2')
  assert.equal(summaries[0].rootEventId, 'evt-3')
  assert.equal(summaries[0].hasFallback, true)
  assert.equal(summaries[0].isActive, true)
  assert.equal(summaries[0].primaryAgentId, 'lighting_agent')
  assert.equal(summaries[1].rootEventId, 'evt-1')
})

test('pickDefaultEpisode 会优先选择最新活跃 episode，否则回退到最近完成的一条', () => {
  const inactive = buildEpisodeSummaries([
    makeEvent({ event_id: 'evt-1', event_type: 'user.activity_change', correlation_id: 'ep-1', wall_time: 10 }),
    makeEvent({ event_id: 'evt-2', event_type: 'user.activity_change', correlation_id: 'ep-2', wall_time: 20 }),
  ])

  assert.equal(pickDefaultEpisode(inactive)?.correlationId, 'ep-2')

  const active = buildEpisodeSummaries([
    makeEvent({ event_id: 'evt-3', event_type: 'user.activity_change', correlation_id: 'ep-3', wall_time: 30 }),
    makeEvent({ event_id: 'evt-4', event_type: 'user.activity_change', correlation_id: 'ep-4', wall_time: 40 }),
  ], makeAgents('ep-3'))

  assert.equal(pickDefaultEpisode(active)?.correlationId, 'ep-3')
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

  assert.deepEqual(
    nodes.map((node) => [node.eventId, node.depth, node.isRoot]),
    [
      ['evt-root', 0, true],
      ['evt-intent', 1, false],
      ['evt-action', 2, false],
      ['evt-feedback', 3, false],
    ],
  )
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

  assert.equal(intentDetail.kind, 'reasoning')
  assert.equal(intentDetail.title, '意图识别')
  assert.equal(intentDetail.fields.find((field) => field.label === '意图')?.value, 'light occupied room')
  assert.equal(intentDetail.reasoningSteps?.[1]?.state, 'current')

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

  assert.equal(feedbackDetail.kind, 'feedback')
  assert.equal(feedbackDetail.fields.find((field) => field.label === '路径')?.value, 'devices[light_living_01].state.power')
  assert.equal(feedbackDetail.fields.find((field) => field.label === '新值')?.value, 'true')
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

  assert.equal(
    deriveObservabilityState({
      connectionState: 'connecting',
      isRunning: false,
      selectedEpisode: null,
      selectedEvent: null,
    }).status,
    'loading',
  )

  assert.equal(
    deriveObservabilityState({
      connectionState: 'disconnected',
      isRunning: true,
      selectedEpisode: episode,
      selectedEvent: episode.events[0],
    }).status,
    'disconnected',
  )

  assert.equal(
    deriveObservabilityState({
      connectionState: 'connected',
      isRunning: false,
      selectedEpisode: null,
      selectedEvent: null,
    }).status,
    'needs_start',
  )

  const ready = deriveObservabilityState({
    connectionState: 'connected',
    isRunning: true,
    selectedEpisode: episode,
    selectedEvent: null,
  })

  assert.equal(ready.status, 'empty')
  assert.equal(ready.fallbackMessage, 'LLM 超时，已回退规则链路')
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

  assert.equal(summaries.length, 500)
  assert.equal(summaries[0].correlationId, 'ep-501')
  assert.equal(summaries[summaries.length - 1].correlationId, 'ep-2')
})
