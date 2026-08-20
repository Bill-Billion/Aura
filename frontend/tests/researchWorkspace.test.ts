import { effectScope } from 'vue'
import { useResearchWorkspace } from '../src/composables/useResearchWorkspace.ts'

afterEach(() => vi.unstubAllGlobals())

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

function runMetadata(ended: boolean, overrides: Record<string, unknown> = {}) {
  return {
    run_id: 'run-a',
    scenario_id: 'morning_wake_up',
    seed: 1004,
    started_at: '2026-08-20T01:00:00Z',
    ended_at: ended ? '2026-08-20T01:01:00Z' : null,
    end_reason: ended ? 'completed' : null,
    event_count: ended ? 1 : 0,
    baseline_policy: 'rule_based',
    recording_source_run_id: null,
    llm_mode: 'rule_based',
    llm_provider: 'disabled',
    llm_model: 'rule_based',
    sim_version: '0.1.3.12',
    source_revision: 'sha256:source-a',
    initial_state_hash: 'abc',
    ...overrides,
  }
}

function report() {
  const entries = {
    episode_complete: true,
    first_action_latency_ms: 10,
    command_failure_count: 0,
    fallback_count: 0,
    conflict_count: 0,
    user_intent_satisfied: true,
    device_state_match_rate: 1,
  }
  return {
    report_schema_version: '1.0',
    run_id: 'run-a',
    scenario_id: 'morning_wake_up',
    seed: 1004,
    outcome: 'pass',
    metrics: Object.fromEntries(Object.entries(entries).map(([name, value]) => [
      name,
      { name, value, unit: '', details: {} },
    ])),
    criteria_checks: {},
    failure_reasons: [],
    metadata: {},
  }
}

test('POST 后 polling 暂时失败，retry 重新附着已有 run 而不重复创建', async () => {
  let postCalls = 0
  let metadataCalls = 0
  vi.stubGlobal('fetch', async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    if (url === '/api/runs' && init?.method === 'POST') {
      postCalls += 1
      return jsonResponse({ run: runMetadata(false) }, 201)
    }
    if (url === '/api/runs/run-a') {
      metadataCalls += 1
      if (metadataCalls === 1) {
        return jsonResponse({ detail: { code: 'temporary_error', message: '稍后重试' } }, 503)
      }
      return jsonResponse({ run: runMetadata(true), event_count: 1, artifacts: [] })
    }
    if (url.startsWith('/api/runs/run-a/events?')) {
      return jsonResponse({
        run_id: 'run-a',
        count: 1,
        total: 1,
        offset: 0,
        events: [{
          seq: 0,
          event_id: 'event-0',
          event_type: 'system.simulation_reset',
          source: 'engine',
          timestamp: 0,
          wall_time: 1,
          correlation_id: 'episode-0',
          causal_parent: null,
          priority: 1,
          data: {},
        }],
      })
    }
    if (url === '/api/runs/run-a/report') return jsonResponse(report())
    if (url === '/api/runs?limit=100') return jsonResponse({ count: 1, runs: [runMetadata(true)] })
    throw new Error(`unexpected request: ${url}`)
  })

  const scope = effectScope()
  const workspace = scope.run(() => useResearchWorkspace())
  expect(workspace).toBeDefined()
  if (!workspace) return
  workspace.form.scenarioId = 'morning_wake_up'
  workspace.form.seed = 1004

  await workspace.startSide('A')
  expect(workspace.slotA.phase).toBe('error')
  expect(workspace.slotA.run?.run_id).toBe('run-a')
  expect(postCalls).toBe(1)

  await workspace.retrySide('A')
  expect(workspace.slotA.phase).toBe('success')
  expect(workspace.slotA.run?.run_id).toBe('run-a')
  expect(postCalls).toBe(1)
  scope.stop()
})

test('201 丢失后的 run_already_active 可核对契约并附着 active_run_id', async () => {
  let postCalls = 0
  vi.stubGlobal('fetch', async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    if (url === '/api/runs' && init?.method === 'POST') {
      postCalls += 1
      return jsonResponse({
        detail: {
          code: 'run_already_active',
          message: '已有 canonical run',
          details: { active_run_id: 'run-a' },
        },
      }, 409)
    }
    if (url === '/api/runs/run-a') {
      return jsonResponse({ run: runMetadata(true), event_count: 1, artifacts: [] })
    }
    if (url.startsWith('/api/runs/run-a/events?')) {
      return jsonResponse({ run_id: 'run-a', count: 0, total: 0, offset: 0, events: [] })
    }
    if (url === '/api/runs/run-a/report') return jsonResponse(report())
    if (url === '/api/runs?limit=100') return jsonResponse({ count: 1, runs: [runMetadata(true)] })
    throw new Error(`unexpected request: ${url}`)
  })

  const scope = effectScope()
  const workspace = scope.run(() => useResearchWorkspace())
  expect(workspace).toBeDefined()
  if (!workspace) return
  workspace.form.scenarioId = 'morning_wake_up'
  workspace.form.seed = 1004

  await workspace.startSide('A')
  expect(workspace.slotA.phase).toBe('success')
  expect(workspace.slotA.run?.run_id).toBe('run-a')
  expect(postCalls).toBe(1)
  scope.stop()
})

test('201 丢失时不会附着 recording source 不同的 active run', async () => {
  vi.stubGlobal('fetch', async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    if (url === '/api/runs' && init?.method === 'POST') {
      return jsonResponse({
        detail: {
          code: 'run_already_active',
          message: '已有 canonical run',
          details: { active_run_id: 'run-replay' },
        },
      }, 409)
    }
    if (url === '/api/runs/run-replay') {
      return jsonResponse({
        run: runMetadata(true, {
          run_id: 'run-replay',
          baseline_policy: 'llm_recorded',
          llm_mode: 'recorded',
          recording_source_run_id: 'source-run',
        }),
        event_count: 1,
        artifacts: [],
      })
    }
    throw new Error(`unexpected request: ${url}`)
  })

  const scope = effectScope()
  const workspace = scope.run(() => useResearchWorkspace())
  expect(workspace).toBeDefined()
  if (!workspace) return
  workspace.form.scenarioId = 'morning_wake_up'
  workspace.form.seed = 1004
  workspace.form.policyA = 'llm_recorded'
  workspace.form.recordingSourceA = ''

  await workspace.startSide('A')

  expect(workspace.slotA.phase).toBe('error')
  expect(workspace.slotA.run).toBeNull()
  expect(workspace.slotA.error?.code).toBe('run_already_active')
  scope.stop()
})

test('网络失败后的显式重试复用同一个 idempotency key', async () => {
  const postBodies: Array<Record<string, unknown>> = []
  vi.stubGlobal('fetch', async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    if (url === '/api/runs' && init?.method === 'POST') {
      postBodies.push(JSON.parse(String(init.body)))
      if (postBodies.length === 1) throw new Error('201 response lost')
      return jsonResponse({ run: runMetadata(true) }, 201)
    }
    if (url === '/api/runs/run-a') {
      return jsonResponse({ run: runMetadata(true), event_count: 0, artifacts: [] })
    }
    if (url.startsWith('/api/runs/run-a/events?')) {
      return jsonResponse({ run_id: 'run-a', count: 0, total: 0, offset: 0, events: [] })
    }
    if (url === '/api/runs/run-a/report') return jsonResponse(report())
    if (url === '/api/runs?limit=100') return jsonResponse({ count: 1, runs: [runMetadata(true)] })
    throw new Error(`unexpected request: ${url}`)
  })

  const scope = effectScope()
  const workspace = scope.run(() => useResearchWorkspace())
  expect(workspace).toBeDefined()
  if (!workspace) return
  workspace.form.scenarioId = 'morning_wake_up'
  workspace.form.seed = 1004

  await workspace.startSide('A')
  expect(workspace.slotA.phase).toBe('error')
  await workspace.retrySide('A')

  expect(workspace.slotA.phase).toBe('success')
  expect(postBodies).toHaveLength(2)
  expect(postBodies[0].idempotency_key).toMatch(/^[0-9a-f-]{36}$/)
  expect(postBodies[1].idempotency_key).toBe(postBodies[0].idempotency_key)
  scope.stop()
})
