import type { EvalReport, RunSummary } from '../src/types/eval-report.ts'
import type { RawRunEvent } from '../src/types/research-run.ts'
import {
  alignRunEvents,
  buildComparisonBundle,
  buildComparisonFilename,
  buildTraceFilename,
  checkComparisonInvariant,
  compareReports,
  filterEventsByDeviceCorrelation,
  recordingSourceCandidates,
  serializeEventsJsonl,
} from '../src/utils/runComparison.ts'

function makeRun(overrides: Partial<RunSummary> = {}): RunSummary {
  return {
    run_id: 'run-a',
    scenario_id: 'morning_wake_up',
    seed: 1004,
    started_at: '2026-08-20T01:00:00Z',
    ended_at: '2026-08-20T01:01:00Z',
    end_reason: 'completed',
    event_count: 3,
    baseline_policy: 'rule_based',
    llm_mode: 'rule_based',
    llm_provider: 'disabled',
    llm_model: 'rule_based',
    sim_version: '0.1.3.12',
    source_revision: 'sha256:source-a',
    agent_versions: {
      coordination_agent: '0.1.3.12',
      lighting_agent: '0.1.3.12',
    },
    initial_state_hash: 'abc123',
    scenario_contract_hash: 'contract-abc123',
    scenario_schema_version: '1.1',
    event_schema_version: '1.0',
    command_schema_version: '1.0',
    device_registry_version: '1.0',
    ...overrides,
  }
}

function makeReport(runId: string, overrides: Partial<Record<string, number | boolean>> = {}): EvalReport {
  const values = {
    episode_complete: true,
    first_action_latency_ms: 200,
    command_failure_count: 1,
    fallback_count: 0,
    conflict_count: 1,
    user_intent_satisfied: true,
    device_state_match_rate: 0.75,
    ...overrides,
  }
  return {
    report_schema_version: '1.0',
    run_id: runId,
    scenario_id: 'morning_wake_up',
    seed: 1004,
    outcome: 'pass',
    metrics: Object.fromEntries(Object.entries(values).map(([name, value]) => [
      name,
      { name, value, unit: name.endsWith('_ms') ? 'ms' : '', details: {} },
    ])) as EvalReport['metrics'],
    criteria_checks: {},
    failure_reasons: [],
    provenance: {
      scenario_contract_hash: 'contract-abc123',
      source_revision: 'sha256:source-a',
      evaluator_source_revision: 'sha256:evaluator-current',
    },
    metadata: {},
  }
}

function makeEvent(seq: number, eventType: string, data: Record<string, unknown> = {}): RawRunEvent {
  return {
    seq,
    event_id: `event-${seq}`,
    event_type: eventType,
    source: 'test',
    timestamp: seq,
    wall_time: seq,
    correlation_id: `correlation-${Math.floor(seq / 10)}`,
    causal_parent: seq > 1 ? `event-${seq - 1}` : null,
    priority: 1,
    data,
  }
}

test('比较硬 invariant 同时约束不同 run、同 scenario 与同 seed', () => {
  const runA = makeRun()
  expect(checkComparisonInvariant(runA, makeRun()).code).toBe('same_run')
  expect(checkComparisonInvariant(runA, makeRun({ run_id: 'run-b', scenario_id: 'leave_home' })).code)
    .toBe('scenario_mismatch')
  expect(checkComparisonInvariant(runA, makeRun({ run_id: 'run-b', seed: 1005 })).code)
    .toBe('seed_mismatch')
  expect(checkComparisonInvariant(
    runA,
    makeRun({ run_id: 'run-b' }),
    makeReport('run-a'),
    makeReport('run-b'),
  )).toMatchObject({ ok: true, code: 'ready' })
})

test.each([
  ['initial_state_hash', { initial_state_hash: 'different-hash' }],
  ['scenario_contract_hash', { scenario_contract_hash: 'different-contract' }],
  ['sim_version', { sim_version: '0.1.3.13' }],
  ['source_revision', { source_revision: 'sha256:source-b' }],
  ['agent_versions', { agent_versions: { lighting_agent: '0.1.3.13' } }],
  ['scenario_schema_version', { scenario_schema_version: '2.0' }],
  ['event_schema_version', { event_schema_version: '2.0' }],
  ['command_schema_version', { command_schema_version: '2.0' }],
  ['device_registry_version', { device_registry_version: '2.0' }],
] satisfies Array<[string, Partial<RunSummary>]>)('比较拒绝不一致的 %s', (field, override) => {
  const result = checkComparisonInvariant(
    makeRun(),
    makeRun({ run_id: 'run-b', ...override }),
    makeReport('run-a'),
    makeReport('run-b'),
  )

  expect(result).toMatchObject({ ok: false, code: 'provenance_mismatch' })
  expect(result.fields).toContain(field)
})

test('比较拒绝 report schema 不一致与缺失 provenance，不默认兼容 legacy run', () => {
  const mismatch = checkComparisonInvariant(
    makeRun(),
    makeRun({ run_id: 'run-b' }),
    makeReport('run-a'),
    { ...makeReport('run-b'), report_schema_version: '2.0' },
  )
  expect(mismatch).toMatchObject({ ok: false, code: 'provenance_mismatch' })
  expect(mismatch.fields).toContain('report_schema_version')

  const missing = checkComparisonInvariant(
    makeRun(),
    makeRun({ run_id: 'run-b', agent_versions: {} }),
    makeReport('run-a'),
    makeReport('run-b'),
  )
  expect(missing).toMatchObject({ ok: false, code: 'provenance_missing' })
  expect(missing.fields).toContain('Run B agent_versions')

  const missingReport = checkComparisonInvariant(
    makeRun(),
    makeRun({ run_id: 'run-b' }),
    makeReport('run-a'),
    null,
  )
  expect(missingReport).toMatchObject({ ok: false, code: 'provenance_missing' })
  expect(missingReport.fields).toContain('Run B report_schema_version')
})

test('场景合约 hash 在 run 和 report provenance 中都必须完整且一致', () => {
  const missingRunHash = checkComparisonInvariant(
    makeRun(),
    makeRun({ run_id: 'run-b', scenario_contract_hash: null }),
    makeReport('run-a'),
    makeReport('run-b'),
  )
  expect(missingRunHash).toMatchObject({ ok: false, code: 'provenance_missing' })
  expect(missingRunHash.fields).toContain('Run B scenario_contract_hash')

  const missingReportHash = checkComparisonInvariant(
    makeRun(),
    makeRun({ run_id: 'run-b' }),
    makeReport('run-a'),
    { ...makeReport('run-b'), provenance: {} },
  )
  expect(missingReportHash).toMatchObject({ ok: false, code: 'provenance_missing' })
  expect(missingReportHash.fields).toContain('Run B report provenance.scenario_contract_hash')

  const reportRunMismatch = checkComparisonInvariant(
    makeRun(),
    makeRun({ run_id: 'run-b' }),
    makeReport('run-a'),
    {
      ...makeReport('run-b'),
      provenance: {
        scenario_contract_hash: 'different-contract',
        source_revision: 'sha256:source-a',
        evaluator_source_revision: 'sha256:evaluator-current',
      },
    },
  )
  expect(reportRunMismatch).toMatchObject({ ok: false, code: 'provenance_mismatch' })
  expect(reportRunMismatch.fields).toContain('Run B report provenance.scenario_contract_hash')

  const sourceMismatch = checkComparisonInvariant(
    makeRun(),
    makeRun({ run_id: 'run-b' }),
    makeReport('run-a'),
    {
      ...makeReport('run-b'),
      provenance: {
        scenario_contract_hash: 'contract-abc123',
        source_revision: 'sha256:source-b',
        evaluator_source_revision: 'sha256:evaluator-current',
      },
    },
  )
  expect(sourceMismatch).toMatchObject({ ok: false, code: 'provenance_mismatch' })
  expect(sourceMismatch.fields).toContain('Run B report provenance.source_revision')
})

test('策略、provider 与 effective mode 可以不同，其余 provenance 一致时可比较', () => {
  const result = checkComparisonInvariant(
    makeRun(),
    makeRun({
      run_id: 'run-b',
      baseline_policy: 'llm_live',
      llm_provider: 'openai',
      llm_model: 'gpt-test',
      llm_mode: 'live',
    }),
    makeReport('run-a'),
    makeReport('run-b'),
  )

  expect(result).toMatchObject({ ok: true, code: 'ready' })
})

test('评估器 revision 可不同于运行 revision，但两份报告必须使用同一评估器', () => {
  const ready = checkComparisonInvariant(
    makeRun(),
    makeRun({ run_id: 'run-b' }),
    makeReport('run-a'),
    makeReport('run-b'),
  )
  expect(ready).toMatchObject({ ok: true, code: 'ready' })

  const mismatched = checkComparisonInvariant(
    makeRun(),
    makeRun({ run_id: 'run-b' }),
    makeReport('run-a'),
    {
      ...makeReport('run-b'),
      provenance: {
        ...makeReport('run-b').provenance,
        evaluator_source_revision: 'sha256:evaluator-next',
      },
    },
  )
  expect(mismatched).toMatchObject({ ok: false, code: 'provenance_mismatch' })
  expect(mismatched.fields).toContain('evaluator_source_revision')

  const missingReport = makeReport('run-b')
  delete missingReport.provenance?.evaluator_source_revision
  const missing = checkComparisonInvariant(
    makeRun(),
    makeRun({ run_id: 'run-b' }),
    makeReport('run-a'),
    missingReport,
  )
  expect(missing).toMatchObject({ ok: false, code: 'provenance_missing' })
  expect(missing.fields).toContain('Run B report provenance.evaluator_source_revision')
})

test('recorded source 候选排除由 recording_source_run_id 回放派生的 run', () => {
  const original = makeRun({
    run_id: 'recording-original',
    baseline_policy: 'llm_recorded',
    llm_mode: 'recorded',
    recording_source_run_id: null,
  })
  const replay = makeRun({
    run_id: 'recording-replay',
    baseline_policy: 'llm_recorded',
    llm_mode: 'recorded',
    recording_source_run_id: original.run_id,
  })
  const unfinished = makeRun({
    run_id: 'recording-active',
    baseline_policy: 'llm_recorded',
    llm_mode: 'recorded',
    recording_source_run_id: null,
    ended_at: null,
  })

  expect(recordingSourceCandidates(
    [replay, unfinished, original, makeRun({ run_id: 'wrong-mode' })],
    'morning_wake_up',
    1004,
  ).map((run) => run.run_id)).toEqual(['recording-original'])
})

test('七项 canonical metrics 计算 A/B delta，并按指标方向判断胜者', () => {
  const rows = compareReports(
    makeReport('run-a'),
    makeReport('run-b', {
      first_action_latency_ms: 150,
      command_failure_count: 2,
      device_state_match_rate: 0.9,
    }),
  )

  expect(rows.map((row) => row.definition.key)).toEqual([
    'episode_complete',
    'first_action_latency_ms',
    'command_failure_count',
    'fallback_count',
    'conflict_count',
    'user_intent_satisfied',
    'device_state_match_rate',
  ])
  expect(rows.find((row) => row.definition.key === 'first_action_latency_ms')).toMatchObject({ delta: -50, winner: 'B' })
  expect(rows.find((row) => row.definition.key === 'command_failure_count')).toMatchObject({ delta: 1, winner: 'A' })
  expect(rows.find((row) => row.definition.key === 'conflict_count')).toMatchObject({ winner: 'unavailable' })
  expect(rows.find((row) => row.definition.key === 'device_state_match_rate')?.delta).toBeCloseTo(0.15)
  expect(rows.find((row) => row.definition.key === 'device_state_match_rate')?.winner).toBe('B')
})

test('因果时间线用语义 occurrence 生成稳定 alignment key，并显式保留 gap', () => {
  const rootA = makeEvent(1, 'user.command', { device_id: 'light_1', action: 'turn_on' })
  const actionA = makeEvent(3, 'action.device_control', { device_id: 'light_1', property: 'power', value: true })
  const rootB = { ...makeEvent(10, 'user.command', { device_id: 'light_1', action: 'turn_on' }), event_id: 'b-root' }
  const fallbackB = { ...makeEvent(11, 'reasoning.fallback_rule_based', { agent_id: 'lighting_agent' }), event_id: 'b-fallback', causal_parent: 'b-root' }
  const actionB = { ...makeEvent(12, 'action.device_control', { device_id: 'light_1', property: 'power', value: false }), event_id: 'b-action', causal_parent: 'b-fallback' }

  const rows = alignRunEvents([actionA, rootA], [actionB, rootB, fallbackB])
  const matched = rows.filter((row) => row.kind === 'match')
  const gaps = rows.filter((row) => row.kind !== 'match')

  expect(matched).toHaveLength(2)
  expect(matched[0].eventA?.alignmentKey).toBe(matched[0].eventB?.alignmentKey)
  expect(matched[1].eventA?.event.data).toMatchObject({ value: true })
  expect(matched[1].eventB?.event.data).toMatchObject({ value: false })
  expect(gaps).toHaveLength(1)
  expect(gaps[0]).toMatchObject({ kind: 'gap-a', eventA: null })
})

test('一侧较早多一个不同 episode 时，后续同构 episode 不会因全局 occurrence 串轨', () => {
  const extraRoot = { ...makeEvent(1, 'user.activity_change', { user_id: 'user_1', to_room: 'kitchen' }), correlation_id: 'extra' }
  const extraAction = { ...makeEvent(2, 'action.device_control', { device_id: 'kitchen_light', property: 'power' }), correlation_id: 'extra' }
  const rootA = { ...makeEvent(10, 'user.command', { device_id: 'living_light', action: 'turn_on' }), correlation_id: 'target-a' }
  const actionA = { ...makeEvent(11, 'action.device_control', { device_id: 'living_light', property: 'power' }), correlation_id: 'target-a' }
  const rootB = { ...makeEvent(20, 'user.command', { device_id: 'living_light', action: 'turn_on' }), correlation_id: 'target-b' }
  const actionB = { ...makeEvent(21, 'action.device_control', { device_id: 'living_light', property: 'power' }), correlation_id: 'target-b' }

  const rows = alignRunEvents([extraRoot, extraAction, rootA, actionA], [rootB, actionB])
  const targetRows = rows.filter((row) => row.eventA?.event.correlation_id === 'target-a')
  expect(targetRows).toHaveLength(2)
  expect(targetRows.every((row) => row.kind === 'match')).toBe(true)
  expect(targetRows.map((row) => row.eventB?.event.correlation_id)).toEqual(['target-b', 'target-b'])
})

test('3D 设备过滤保留命中设备所在 correlation 的完整根到动作因果链', () => {
  const root = { ...makeEvent(1, 'user.command', { message_type: 'CMD_RUN' }), correlation_id: 'target' }
  const reasoning = { ...makeEvent(2, 'reasoning.intent_recognized', { agent_id: 'lighting_agent' }), correlation_id: 'target' }
  const action = { ...makeEvent(3, 'action.device_control', { device_id: 'living_light', property: 'power' }), correlation_id: 'target' }
  const unrelated = { ...makeEvent(4, 'action.device_control', { device_id: 'kitchen_light', property: 'power' }), correlation_id: 'other' }

  const filtered = filterEventsByDeviceCorrelation([root, reasoning, action, unrelated], 'living_light')
  expect(filtered.map((event) => event.event_id)).toEqual([root.event_id, reasoning.event_id, action.event_id])
})

test('JSONL 与 comparison 包始终按 seq 导出 raw events，文件名可移植', () => {
  const event1 = makeEvent(1, 'user.command')
  const event2 = makeEvent(2, 'action.device_control')
  const jsonl = serializeEventsJsonl([event2, event1])
  expect(jsonl.split('\n').filter(Boolean).map((line) => JSON.parse(line).seq)).toEqual([1, 2])
  expect(jsonl).not.toContain('alignmentKey')

  const runA = makeRun({ run_id: 'run:a' })
  const runB = makeRun({ run_id: 'run/b' })
  const bundle = buildComparisonBundle({
    runA,
    reportA: makeReport(runA.run_id),
    eventsA: [event2, event1],
    runB,
    reportB: makeReport(runB.run_id),
    eventsB: [event1],
    exportedAt: '2026-08-20T02:00:00.000Z',
  })

  expect(bundle.invariant).toEqual({ scenario_id: 'morning_wake_up', seed: 1004 })
  expect(bundle.run_a.events.map((event) => event.seq)).toEqual([1, 2])
  expect(buildTraceFilename(runA)).toMatch(/\.events\.jsonl$/)
  expect(buildTraceFilename(runA)).not.toContain(':')
  expect(buildComparisonFilename(runA, runB)).not.toContain('/')
  expect(buildComparisonFilename(runA, runB)).toContain('rule_based-vs-rule_based')
})
