import type {
  CanonicalMetricKey,
  EvalReport,
  MetricDatum,
  RunSummary,
} from '@/types/eval-report'
import type {
  RawRunEvent,
  RunLaunchConfig,
  RunSide,
} from '@/types/research-run'

export interface CanonicalMetricDefinition {
  key: CanonicalMetricKey
  label: string
  shortLabel: string
  direction: 'higher' | 'lower' | 'informational'
  boolean: boolean
  fallbackUnit: string
}

export const CANONICAL_METRICS: readonly CanonicalMetricDefinition[] = [
  {
    key: 'episode_complete',
    label: 'Episode 完整',
    shortLabel: '完整',
    direction: 'higher',
    boolean: true,
    fallbackUnit: '',
  },
  {
    key: 'first_action_latency_ms',
    label: '首次动作延迟',
    shortLabel: '延迟',
    direction: 'lower',
    boolean: false,
    fallbackUnit: 'ms',
  },
  {
    key: 'command_failure_count',
    label: '命令失败数',
    shortLabel: '失败',
    direction: 'lower',
    boolean: false,
    fallbackUnit: 'count',
  },
  {
    key: 'fallback_count',
    label: '规则回退数',
    shortLabel: '回退',
    direction: 'lower',
    boolean: false,
    fallbackUnit: 'count',
  },
  {
    key: 'conflict_count',
    label: '协调冲突数',
    shortLabel: '冲突',
    direction: 'informational',
    boolean: false,
    fallbackUnit: 'count',
  },
  {
    key: 'user_intent_satisfied',
    label: '用户意图满足',
    shortLabel: '意图',
    direction: 'higher',
    boolean: true,
    fallbackUnit: '',
  },
  {
    key: 'device_state_match_rate',
    label: '设备状态匹配率',
    shortLabel: '状态',
    direction: 'higher',
    boolean: false,
    fallbackUnit: '%',
  },
] as const

export type MetricWinner = RunSide | 'tie' | 'unavailable'

export interface MetricComparisonRow {
  definition: CanonicalMetricDefinition
  metricA: MetricDatum | null
  metricB: MetricDatum | null
  numericA: number | null
  numericB: number | null
  delta: number | null
  winner: MetricWinner
  unit: string
}

export interface ComparisonInvariantResult {
  ok: boolean
  code:
    | 'ready'
    | 'missing_run'
    | 'same_run'
    | 'scenario_mismatch'
    | 'seed_mismatch'
    | 'provenance_missing'
    | 'provenance_mismatch'
  message: string
  fields?: string[]
}

export interface AlignmentEvent {
  event: RawRunEvent
  alignmentKey: string
  semanticKey: string
  causalDepth: number
}

export interface EventAlignmentRow {
  key: string
  eventA: AlignmentEvent | null
  eventB: AlignmentEvent | null
  kind: 'match' | 'gap-a' | 'gap-b'
}

export interface ComparisonExportBundle {
  schema_version: 'aura.run_comparison.v1'
  exported_at: string
  invariant: {
    scenario_id: string
    seed: number
  }
  run_a: {
    provenance: RunSummary
    report: EvalReport
    events: RawRunEvent[]
  }
  run_b: {
    provenance: RunSummary
    report: EvalReport
    events: RawRunEvent[]
  }
}

export function compareReports(
  reportA: EvalReport | null,
  reportB: EvalReport | null,
): MetricComparisonRow[] {
  return CANONICAL_METRICS.map((definition) => {
    const metricA = reportA?.metrics?.[definition.key] ?? null
    const metricB = reportB?.metrics?.[definition.key] ?? null
    const numericA = metricValueAsNumber(metricA?.value)
    const numericB = metricValueAsNumber(metricB?.value)
    const delta = numericA === null || numericB === null ? null : numericB - numericA

    let winner: MetricWinner = 'unavailable'
    if (numericA !== null && numericB !== null && definition.direction !== 'informational') {
      if (Math.abs(numericA - numericB) <= Number.EPSILON) {
        winner = 'tie'
      } else if (definition.direction === 'higher') {
        winner = numericA > numericB ? 'A' : 'B'
      } else {
        winner = numericA < numericB ? 'A' : 'B'
      }
    }

    return {
      definition,
      metricA,
      metricB,
      numericA,
      numericB,
      delta,
      winner,
      unit: metricA?.unit || metricB?.unit || definition.fallbackUnit,
    }
  })
}

export function checkComparisonInvariant(
  runA: RunSummary | null,
  runB: RunSummary | null,
  reportA: EvalReport | null = null,
  reportB: EvalReport | null = null,
): ComparisonInvariantResult {
  if (!runA || !runB) {
    return { ok: false, code: 'missing_run', message: 'Run A 与 Run B 都完成后才能比较。' }
  }
  if (runA.run_id === runB.run_id) {
    return { ok: false, code: 'same_run', message: '请选择两次不同的 run。' }
  }
  if (!runA.scenario_id || runA.scenario_id !== runB.scenario_id) {
    return {
      ok: false,
      code: 'scenario_mismatch',
      message: '比较被阻止：Run A 与 Run B 必须使用同一个 canonical scenario。',
    }
  }
  if (runA.seed === null || runA.seed !== runB.seed) {
    return {
      ok: false,
      code: 'seed_mismatch',
      message: '比较被阻止：Run A 与 Run B 必须使用相同 seed。',
    }
  }

  const provenance = [
    ['initial_state_hash', readNonEmptyString(runA, 'initial_state_hash'), readNonEmptyString(runB, 'initial_state_hash')],
    ['scenario_contract_hash', readNonEmptyString(runA, 'scenario_contract_hash'), readNonEmptyString(runB, 'scenario_contract_hash')],
    ['sim_version', readNonEmptyString(runA, 'sim_version'), readNonEmptyString(runB, 'sim_version')],
    ['source_revision', readNonEmptyString(runA, 'source_revision'), readNonEmptyString(runB, 'source_revision')],
    ['agent_versions', readAgentVersions(runA), readAgentVersions(runB)],
    ['scenario_schema_version', readNonEmptyString(runA, 'scenario_schema_version'), readNonEmptyString(runB, 'scenario_schema_version')],
    ['event_schema_version', readNonEmptyString(runA, 'event_schema_version'), readNonEmptyString(runB, 'event_schema_version')],
    ['command_schema_version', readNonEmptyString(runA, 'command_schema_version'), readNonEmptyString(runB, 'command_schema_version')],
    ['device_registry_version', readNonEmptyString(runA, 'device_registry_version'), readNonEmptyString(runB, 'device_registry_version')],
    ['report_schema_version', readReportSchema(reportA), readReportSchema(reportB)],
  ] as const
  const missing = provenance.flatMap(([field, valueA, valueB]) => [
    ...(valueA === null ? [`Run A ${field}`] : []),
    ...(valueB === null ? [`Run B ${field}`] : []),
  ])
  if (!reportMatchesRun(reportA, runA)) missing.push('Run A report binding')
  if (!reportMatchesRun(reportB, runB)) missing.push('Run B report binding')
  const reportContractHashA = readReportScenarioContractHash(reportA)
  const reportContractHashB = readReportScenarioContractHash(reportB)
  const reportSourceRevisionA = readReportSourceRevision(reportA)
  const reportSourceRevisionB = readReportSourceRevision(reportB)
  const evaluatorSourceRevisionA = readReportEvaluatorSourceRevision(reportA)
  const evaluatorSourceRevisionB = readReportEvaluatorSourceRevision(reportB)
  if (reportContractHashA === null) missing.push('Run A report provenance.scenario_contract_hash')
  if (reportContractHashB === null) missing.push('Run B report provenance.scenario_contract_hash')
  if (reportSourceRevisionA === null) missing.push('Run A report provenance.source_revision')
  if (reportSourceRevisionB === null) missing.push('Run B report provenance.source_revision')
  if (evaluatorSourceRevisionA === null) missing.push('Run A report provenance.evaluator_source_revision')
  if (evaluatorSourceRevisionB === null) missing.push('Run B report provenance.evaluator_source_revision')
  if (missing.length > 0) {
    return {
      ok: false,
      code: 'provenance_missing',
      message: `比较被阻止：缺少或无效的 provenance（${missing.join('、')}）。`,
      fields: missing,
    }
  }

  const mismatched: string[] = provenance
    .filter(([, valueA, valueB]) => valueA !== valueB)
    .map(([field]) => field)
  if (reportContractHashA !== readNonEmptyString(runA, 'scenario_contract_hash')) {
    mismatched.push('Run A report provenance.scenario_contract_hash')
  }
  if (reportContractHashB !== readNonEmptyString(runB, 'scenario_contract_hash')) {
    mismatched.push('Run B report provenance.scenario_contract_hash')
  }
  if (reportSourceRevisionA !== readNonEmptyString(runA, 'source_revision')) {
    mismatched.push('Run A report provenance.source_revision')
  }
  if (reportSourceRevisionB !== readNonEmptyString(runB, 'source_revision')) {
    mismatched.push('Run B report provenance.source_revision')
  }
  if (evaluatorSourceRevisionA !== evaluatorSourceRevisionB) {
    mismatched.push('evaluator_source_revision')
  }
  if (mismatched.length > 0) {
    return {
      ok: false,
      code: 'provenance_mismatch',
      message: `比较被阻止：实验 provenance 不一致（${mismatched.join('、')}）。`,
      fields: mismatched,
    }
  }
  return {
    ok: true,
    code: 'ready',
    message: '场景、seed 与实验 provenance 一致，可以进行策略对比。',
  }
}

export function recordingSourceCandidates(
  runs: readonly RunSummary[],
  scenarioId: string,
  seed: number,
): RunSummary[] {
  return runs.filter((run) => (
    run.ended_at !== null
    && run.llm_mode === 'recorded'
    && run.scenario_id === scenarioId
    && run.seed === seed
    && !run.recording_source_run_id
  ))
}

export function launchMatchesRun(
  config: Pick<RunLaunchConfig, 'scenario_id' | 'seed'>,
  run: Pick<RunSummary, 'scenario_id' | 'seed'>,
): boolean {
  return config.scenario_id === run.scenario_id && config.seed === run.seed
}

export function sortRawEvents(events: readonly RawRunEvent[]): RawRunEvent[] {
  return [...events].sort((left, right) => {
    const leftSeq = Number.isFinite(left.seq) ? left.seq : Number.MAX_SAFE_INTEGER
    const rightSeq = Number.isFinite(right.seq) ? right.seq : Number.MAX_SAFE_INTEGER
    if (leftSeq !== rightSeq) return leftSeq - rightSeq
    if (left.timestamp !== right.timestamp) return left.timestamp - right.timestamp
    return left.event_id.localeCompare(right.event_id)
  })
}

export function alignRunEvents(
  eventsA: readonly RawRunEvent[],
  eventsB: readonly RawRunEvent[],
): EventAlignmentRow[] {
  const decoratedA = decorateEvents(sortRawEvents(eventsA))
  const decoratedB = decorateEvents(sortRawEvents(eventsB))
  const indexB = new Map(decoratedB.map((event, index) => [event.alignmentKey, index]))
  const commonPairs = decoratedA.flatMap((event, indexA) => {
    const index = indexB.get(event.alignmentKey)
    return index === undefined ? [] : [{ indexA, indexB: index, key: event.alignmentKey }]
  })
  const anchors = longestIncreasingPairs(commonPairs)
  const rows: EventAlignmentRow[] = []
  let cursorA = 0
  let cursorB = 0

  for (const anchor of anchors) {
    while (cursorA < anchor.indexA) {
      const eventA = decoratedA[cursorA]
      rows.push({ key: `a:${eventA.alignmentKey}`, eventA, eventB: null, kind: 'gap-b' })
      cursorA += 1
    }
    while (cursorB < anchor.indexB) {
      const eventB = decoratedB[cursorB]
      rows.push({ key: `b:${eventB.alignmentKey}`, eventA: null, eventB, kind: 'gap-a' })
      cursorB += 1
    }
    rows.push({
      key: `m:${anchor.key}`,
      eventA: decoratedA[anchor.indexA],
      eventB: decoratedB[anchor.indexB],
      kind: 'match',
    })
    cursorA = anchor.indexA + 1
    cursorB = anchor.indexB + 1
  }

  while (cursorA < decoratedA.length) {
    const eventA = decoratedA[cursorA]
    rows.push({ key: `a:${eventA.alignmentKey}`, eventA, eventB: null, kind: 'gap-b' })
    cursorA += 1
  }
  while (cursorB < decoratedB.length) {
    const eventB = decoratedB[cursorB]
    rows.push({ key: `b:${eventB.alignmentKey}`, eventA: null, eventB, kind: 'gap-a' })
    cursorB += 1
  }

  return rows
}

export function eventReferencesDevice(event: RawRunEvent, deviceId: string): boolean {
  const data = event.data as Record<string, unknown>
  if (data.device_id === deviceId) return true
  if (Array.isArray(data.device_ids) && data.device_ids.includes(deviceId)) return true
  if (typeof data.path === 'string' && data.path.includes(`[${deviceId}]`)) return true
  return false
}

/** Preserve complete causal episodes for a 3D device selection. */
export function filterEventsByDeviceCorrelation(
  events: readonly RawRunEvent[],
  deviceId: string | null,
): RawRunEvent[] {
  if (!deviceId) return [...events]
  const matchingCorrelations = new Set(
    events
      .filter((event) => eventReferencesDevice(event, deviceId))
      .map((event) => event.correlation_id),
  )
  return events.filter((event) => matchingCorrelations.has(event.correlation_id))
}

export function serializeEventsJsonl(events: readonly RawRunEvent[]): string {
  const lines = sortRawEvents(events).map((event) => JSON.stringify(event))
  return lines.length > 0 ? `${lines.join('\n')}\n` : ''
}

export function buildTraceFilename(
  run: Pick<RunSummary, 'run_id' | 'scenario_id' | 'seed' | 'baseline_policy'>,
): string {
  return safeFilename(
    `${run.scenario_id ?? 'anonymous'}-${run.seed ?? 'unknown'}-${run.baseline_policy ?? 'unknown-policy'}-${run.run_id}.events.jsonl`,
  )
}

export function buildComparisonFilename(
  runA: Pick<RunSummary, 'scenario_id' | 'seed' | 'run_id' | 'baseline_policy'>,
  runB: Pick<RunSummary, 'run_id' | 'baseline_policy'>,
): string {
  return safeFilename(
    `${runA.scenario_id ?? 'anonymous'}-${runA.seed ?? 'unknown'}-${runA.baseline_policy ?? 'unknown-policy'}-vs-${runB.baseline_policy ?? 'unknown-policy'}-${runA.run_id}--${runB.run_id}.comparison.json`,
  )
}

export function buildComparisonBundle(args: {
  runA: RunSummary
  reportA: EvalReport
  eventsA: readonly RawRunEvent[]
  runB: RunSummary
  reportB: EvalReport
  eventsB: readonly RawRunEvent[]
  exportedAt?: string
}): ComparisonExportBundle {
  const invariant = checkComparisonInvariant(args.runA, args.runB, args.reportA, args.reportB)
  if (!invariant.ok || !args.runA.scenario_id || args.runA.seed === null) {
    throw new Error(invariant.message)
  }
  return {
    schema_version: 'aura.run_comparison.v1',
    exported_at: args.exportedAt ?? new Date().toISOString(),
    invariant: {
      scenario_id: args.runA.scenario_id,
      seed: args.runA.seed,
    },
    run_a: {
      provenance: args.runA,
      report: args.reportA,
      events: sortRawEvents(args.eventsA),
    },
    run_b: {
      provenance: args.runB,
      report: args.reportB,
      events: sortRawEvents(args.eventsB),
    },
  }
}

export function metricValueAsNumber(value: MetricDatum['value'] | undefined): number | null {
  if (typeof value === 'boolean') return value ? 1 : 0
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

function decorateEvents(events: RawRunEvent[]): AlignmentEvent[] {
  const depths = causalDepths(events)
  const byCorrelation = new Map<string, RawRunEvent[]>()
  for (const event of events) {
    const grouped = byCorrelation.get(event.correlation_id) ?? []
    grouped.push(event)
    byCorrelation.set(event.correlation_id, grouped)
  }

  const rootOccurrences = new Map<string, number>()
  const decoratedById = new Map<string, AlignmentEvent>()
  for (const groupedEvents of byCorrelation.values()) {
    const ordered = sortRawEvents(groupedEvents)
    const ids = new Set(ordered.map((event) => event.event_id))
    const root = ordered.find((event) => !event.causal_parent || !ids.has(event.causal_parent)) ?? ordered[0]
    const rootSemanticKey = semanticEventKey(root)
    const episodeOccurrence = (rootOccurrences.get(rootSemanticKey) ?? 0) + 1
    rootOccurrences.set(rootSemanticKey, episodeOccurrence)
    const episodeKey = `episode:${rootSemanticKey}#${episodeOccurrence}`
    const localOccurrences = new Map<string, number>()

    for (const event of ordered) {
      const semanticKey = semanticEventKey(event)
      const occurrence = (localOccurrences.get(semanticKey) ?? 0) + 1
      localOccurrences.set(semanticKey, occurrence)
      decoratedById.set(event.event_id, {
        event,
        semanticKey,
        alignmentKey: `${episodeKey}/${semanticKey}#${occurrence}`,
        causalDepth: depths.get(event.event_id) ?? Math.max(0, event.depth ?? 0),
      })
    }
  }

  return events.map((event) => decoratedById.get(event.event_id) as AlignmentEvent)
}

function semanticEventKey(event: RawRunEvent): string {
  const data = event.data as Record<string, unknown>
  const subject = firstString(
    data.device_id,
    data.agent_id,
    data.user_id,
    data.scene_id,
    data.message_type,
  )
  const operation = firstString(
    data.property,
    data.capability,
    data.action,
    data.to_room,
    data.failed_step,
  )
  const tick = event.event_type === 'system.timer_tick' && typeof data.tick === 'number'
    ? String(data.tick)
    : ''
  return [event.event_type, subject, operation, tick]
    .map((value) => value.replaceAll('|', '_'))
    .join('|')
}

function causalDepths(events: RawRunEvent[]): Map<string, number> {
  const byId = new Map(events.map((event) => [event.event_id, event]))
  const depths = new Map<string, number>()
  const resolving = new Set<string>()

  const resolve = (event: RawRunEvent): number => {
    const existing = depths.get(event.event_id)
    if (existing !== undefined) return existing
    if (!event.causal_parent || resolving.has(event.event_id)) {
      depths.set(event.event_id, 0)
      return 0
    }
    const parent = byId.get(event.causal_parent)
    if (!parent) {
      const fallback = Math.max(0, event.depth ?? 0)
      depths.set(event.event_id, fallback)
      return fallback
    }
    resolving.add(event.event_id)
    const depth = resolve(parent) + 1
    resolving.delete(event.event_id)
    depths.set(event.event_id, depth)
    return depth
  }

  for (const event of events) resolve(event)
  return depths
}

function longestIncreasingPairs<T extends { indexB: number }>(pairs: T[]): T[] {
  if (pairs.length === 0) return []
  const tails: number[] = []
  const previous = Array.from({ length: pairs.length }, () => -1)

  for (let index = 0; index < pairs.length; index += 1) {
    let low = 0
    let high = tails.length
    while (low < high) {
      const middle = Math.floor((low + high) / 2)
      if (pairs[tails[middle]].indexB < pairs[index].indexB) low = middle + 1
      else high = middle
    }
    if (low > 0) previous[index] = tails[low - 1]
    tails[low] = index
  }

  const result: T[] = []
  let cursor = tails[tails.length - 1]
  while (cursor >= 0) {
    result.push(pairs[cursor])
    cursor = previous[cursor]
  }
  return result.reverse()
}

function firstString(...values: unknown[]): string {
  return values.find((value): value is string => typeof value === 'string' && value.length > 0) ?? ''
}

function readNonEmptyString(value: object, key: string): string | null {
  const candidate = (value as Record<string, unknown>)[key]
  return typeof candidate === 'string' && candidate.trim().length > 0 ? candidate : null
}

function readAgentVersions(value: object): string | null {
  const candidate = (value as Record<string, unknown>).agent_versions
  if (!isRecord(candidate)) return null
  const entries = Object.entries(candidate)
  if (
    entries.length === 0
    || entries.some(([, version]) => typeof version !== 'string' || version.trim().length === 0)
  ) return null
  return JSON.stringify(entries.sort(([left], [right]) => left.localeCompare(right)))
}

function readReportSchema(report: EvalReport | null): string | null {
  return report && typeof report.report_schema_version === 'string' && report.report_schema_version.trim().length > 0
    ? report.report_schema_version
    : null
}

function readReportScenarioContractHash(report: EvalReport | null): string | null {
  const candidate = report?.provenance?.scenario_contract_hash
  return typeof candidate === 'string' && candidate.trim().length > 0 ? candidate : null
}

function readReportSourceRevision(report: EvalReport | null): string | null {
  const candidate = report?.provenance?.source_revision
  return typeof candidate === 'string' && candidate.trim().length > 0 ? candidate : null
}

function readReportEvaluatorSourceRevision(report: EvalReport | null): string | null {
  const candidate = report?.provenance?.evaluator_source_revision
  return typeof candidate === 'string' && candidate.trim().length > 0 ? candidate : null
}

function reportMatchesRun(
  report: EvalReport | null,
  run: Pick<RunSummary, 'run_id' | 'scenario_id' | 'seed'>,
): boolean {
  return Boolean(
    report
    && report.run_id === run.run_id
    && report.scenario_id === run.scenario_id
    && report.seed === run.seed,
  )
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function safeFilename(value: string): string {
  return value.replace(/[^a-zA-Z0-9._-]+/g, '_').replace(/_+/g, '_')
}
