import { createPinia, setActivePinia } from 'pinia'
import { useEventStore } from '../src/stores/eventStore.ts'
import { useSimulationStore } from '../src/stores/simulationStore.ts'
import type { SimEvent } from '../src/types/sim-event.ts'
import { decideRunEvent } from '../src/utils/runEventIsolation.ts'

function makeEvent(overrides: Partial<SimEvent> = {}): SimEvent {
  return {
    event_id: 'event-1',
    event_type: 'user.command',
    source: 'test',
    timestamp: 1,
    wall_time: 1,
    correlation_id: 'episode-1',
    causal_parent: null,
    priority: 1,
    data: {},
    ...overrides,
  }
}

beforeEach(() => setActivePinia(createPinia()))

test('simulationStore 保存完整 run identity，增量 timing status 不擦除 provenance', () => {
  const store = useSimulationStore()
  expect(store.applySimulationStatus({
    run_id: 'run-a',
    scenario_id: 'morning_wake_up',
    seed: 1004,
    baseline_policy: 'llm_mocked',
    llm_mode: 'mocked',
    duration_seconds: 120,
    recording_source_run_id: null,
    finalized: false,
    ended_at: null,
    end_reason: null,
  })).toBe(true)

  store.applySimulationStatus({ is_running: true, speed: 2 })
  expect(store.currentRunId).toBe('run-a')
  expect(store.currentScenarioId).toBe('morning_wake_up')
  expect(store.currentSeed).toBe(1004)
  expect(store.currentBaselinePolicy).toBe('llm_mocked')
  expect(store.currentLlmMode).toBe('mocked')

  expect(store.applySimulationStatus({
    run_id: 'run-a',
    finalized: true,
    ended_at: '2026-08-20T03:00:00Z',
    end_reason: 'completed',
  })).toBe(false)
  expect(store.currentRunFinalized).toBe(true)
  expect(store.currentRunEndReason).toBe('completed')
})

test('reset SIM_EVENT 可先于 status 切换 run，并隔离、拒绝旧 run 后续事件', () => {
  const simulation = useSimulationStore()
  const events = useEventStore()
  simulation.applySimulationStatus({ run_id: 'run-old', scenario_id: 'old', finalized: false })
  events.synchronizeRun('run-old')
  expect(events.appendEvent(makeEvent({ run_id: 'run-old', event_id: 'old-root' }))).toBe(true)

  const reset = makeEvent({
    run_id: 'run-new',
    scenario_id: 'new-scenario',
    event_id: 'new-reset',
    event_type: 'system.simulation_reset',
  })
  expect(events.appendEvent(reset)).toBe(true)
  expect(simulation.currentRunId).toBe('run-new')
  expect(events.eventRunId).toBe('run-new')
  expect(events.events.map((event) => event.event_id)).toEqual(['new-reset'])

  expect(events.appendEvent(makeEvent({ run_id: 'run-old', event_id: 'late-old' }))).toBe(false)
  expect(events.events.map((event) => event.event_id)).toEqual(['new-reset'])

  simulation.applySimulationStatus({
    run_id: 'run-new',
    scenario_id: 'new-scenario',
    seed: 7,
    baseline_policy: 'rule_based',
    llm_mode: 'rule_based',
    finalized: false,
  })
  events.synchronizeRun(simulation.currentRunId)
  expect(events.events.map((event) => event.event_id)).toEqual(['new-reset'])
})

test('run event decision 的未知、同 run、reset 切换与 stale 分支稳定', () => {
  expect(decideRunEvent(null, makeEvent({ run_id: null }))).toBe('accept')
  expect(decideRunEvent(null, makeEvent({ run_id: 'run-a' }))).toBe('switch')
  expect(decideRunEvent('run-a', makeEvent({ run_id: 'run-a' }))).toBe('accept')
  expect(decideRunEvent('run-a', makeEvent({ run_id: 'run-b' }))).toBe('ignore')
  expect(decideRunEvent('run-a', makeEvent({ run_id: 'run-b', event_type: 'system.simulation_reset' }))).toBe('switch')
  expect(decideRunEvent('run-a', makeEvent({ run_id: null }))).toBe('ignore')
})

test('simulationStore 暴露结构化命令错误并支持成功状态后的清理', () => {
  const store = useSimulationStore()
  store.setCommandError({
    code: 'research_run_locked',
    message: 'canonical run 运行中',
    details: { run_id: 'run-a', blocked_command: 'CMD_SIM_RESET' },
  })
  expect(store.lastCommandError).toEqual({
    code: 'research_run_locked',
    message: 'canonical run 运行中',
    details: { run_id: 'run-a', blocked_command: 'CMD_SIM_RESET' },
  })
  store.clearCommandError()
  expect(store.lastCommandError).toBeNull()
})
