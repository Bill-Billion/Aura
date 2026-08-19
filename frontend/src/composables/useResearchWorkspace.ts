import { computed, onScopeDispose, reactive, ref } from 'vue'
import type {
  BaselinePolicy,
  RunLaunchConfig,
  RunSide,
  RunSlot,
  ScenarioSummary,
  WorkspaceView,
} from '@/types/research-run'
import type { RunSummary } from '@/types/eval-report'
import { checkComparisonInvariant, launchMatchesRun } from '@/utils/runComparison'
import {
  abortableDelay,
  createAbortableResource,
  createResearchApi,
  toStructuredApiError,
} from './researchApi'

const POLL_INTERVAL_MS = 1_000
const MAX_POLL_ATTEMPTS = 300

export interface ResearchLaunchForm {
  scenarioId: string
  seed: number
  policyA: BaselinePolicy
  policyB: BaselinePolicy
  recordingSourceA: string
  recordingSourceB: string
}

export function useResearchWorkspace() {
  const api = createResearchApi()
  const scenariosResource = createAbortableResource<ScenarioSummary[]>()
  const runsResource = createAbortableResource<RunSummary[]>()
  const view = ref<WorkspaceView>('setup')
  const initialized = ref(false)
  const announcement = ref('')
  const form = reactive<ResearchLaunchForm>({
    scenarioId: '',
    seed: 0,
    policyA: 'rule_based',
    policyB: 'llm_mocked',
    recordingSourceA: '',
    recordingSourceB: '',
  })
  const slotA = reactive<RunSlot>(emptySlot('A'))
  const slotB = reactive<RunSlot>(emptySlot('B'))
  const pollControllers: Record<RunSide, AbortController | null> = { A: null, B: null }

  const scenarios = computed(() => scenariosResource.state.value.data ?? [])
  const recentRuns = computed(() => runsResource.state.value.data ?? [])
  const selectedScenario = computed(() => (
    scenarios.value.find((scenario) => scenario.id === form.scenarioId) ?? null
  ))
  const comparisonInvariant = computed(() => (
    checkComparisonInvariant(slotA.run, slotB.run, slotA.report, slotB.report)
  ))
  const isBusy = computed(() => [slotA, slotB].some((slot) => (
    slot.phase === 'loading' || slot.phase === 'pending'
  )))
  const activeSide = computed<RunSide | null>(() => {
    if (slotB.phase === 'loading' || slotB.phase === 'pending') return 'B'
    if (slotA.phase === 'loading' || slotA.phase === 'pending') return 'A'
    return null
  })

  async function initialize(): Promise<void> {
    if (initialized.value) return
    initialized.value = true
    const [loadedScenarios] = await Promise.all([
      scenariosResource.load((signal) => api.listScenarios(signal).then((body) => body.scenarios)),
      runsResource.load((signal) => api.listRuns(signal).then((body) => body.runs)),
    ])
    if (loadedScenarios?.length && !form.scenarioId) selectScenario(loadedScenarios[0].id)
  }

  async function retryScenarios(): Promise<void> {
    const loaded = await scenariosResource.load((signal) => (
      api.listScenarios(signal).then((body) => body.scenarios)
    ))
    if (loaded?.length && !form.scenarioId) selectScenario(loaded[0].id)
  }

  async function refreshRecentRuns(): Promise<void> {
    await runsResource.load((signal) => api.listRuns(signal).then((body) => body.runs))
  }

  async function getRawTrace(runId: string): Promise<Blob> {
    return api.getRawTrace(runId)
  }

  function selectScenario(scenarioId: string): void {
    form.scenarioId = scenarioId
    const scenario = scenarios.value.find((entry) => entry.id === scenarioId)
    if (scenario) form.seed = scenario.seed
  }

  function copyAParameters(): void {
    const config = slotA.config
    const run = slotA.run
    if (!config && !run) return
    form.scenarioId = config?.scenario_id ?? run?.scenario_id ?? form.scenarioId
    form.seed = config?.seed ?? run?.seed ?? form.seed
    announcement.value = `已复制 Run A 的场景与 seed；Run B 策略仍为 ${form.policyB}。`
  }

  function buildConfig(side: RunSide): RunLaunchConfig {
    const policy = side === 'A' ? form.policyA : form.policyB
    const source = side === 'A' ? form.recordingSourceA : form.recordingSourceB
    return {
      scenario_id: form.scenarioId,
      seed: Number(form.seed),
      baseline_policy: policy,
      idempotency_key: crypto.randomUUID(),
      ...(policy === 'llm_recorded' && source ? { recording_source_run_id: source } : {}),
    }
  }

  async function startSide(side: RunSide, retryConfig?: RunLaunchConfig): Promise<void> {
    const target = side === 'A' ? slotA : slotB
    const config = retryConfig ?? buildConfig(side)
    const invalid = validateConfig(config, side)
    if (invalid) {
      Object.assign(target, {
        phase: 'error',
        stage: '启动参数校验失败',
        config,
        run: null,
        report: null,
        events: [],
        error: invalid,
      } satisfies Partial<RunSlot>)
      announcement.value = invalid.message
      return
    }

    if (side === 'A') {
      abortSlot('B')
      Object.assign(slotB, emptySlot('B'))
    }
    abortSlot(side)
    const controller = new AbortController()
    pollControllers[side] = controller
    Object.assign(target, {
      phase: 'loading',
      stage: '正在创建 run',
      config,
      run: null,
      report: null,
      events: [],
      error: null,
    } satisfies Partial<RunSlot>)
    view.value = 'live'
    announcement.value = `正在启动 Run ${side}。`

    try {
      const response = await api.startRun(config, controller.signal)
      target.run = response.run
      target.phase = 'pending'
      target.stage = '场景运行中'
      announcement.value = `Run ${side} 已创建，正在收集事件。`
      await pollUntilComplete(target, controller.signal)
      finishSide(side)
      await refreshRecentRuns()
    } catch (error) {
      if (controller.signal.aborted) return
      let failure = error
      if (!target.run) {
        try {
          const attached = await tryAttachActiveRun(target, config, error, controller.signal)
          if (attached) {
            finishSide(side)
            await refreshRecentRuns()
            return
          }
        } catch (attachError) {
          failure = attachError
        }
      }
      target.phase = 'error'
      target.stage = target.run ? '收集运行工件失败' : '创建 run 失败'
      target.error = toStructuredApiError(failure)
      announcement.value = target.error.message
    } finally {
      if (pollControllers[side] === controller) pollControllers[side] = null
    }
  }

  async function retrySide(side: RunSide): Promise<void> {
    const slot = side === 'A' ? slotA : slotB
    if (!slot.config) return
    if (!slot.run) {
      await startSide(side, { ...slot.config })
      return
    }

    abortSlot(side)
    const controller = new AbortController()
    pollControllers[side] = controller
    slot.phase = 'pending'
    slot.stage = '正在重新附着原 run'
    slot.error = null
    view.value = 'live'
    announcement.value = `正在重新读取 Run ${side} · ${slot.run.run_id}，不会创建新 run。`
    try {
      await pollUntilComplete(slot, controller.signal)
      finishSide(side)
      await refreshRecentRuns()
    } catch (error) {
      if (controller.signal.aborted) return
      slot.phase = 'error'
      slot.stage = '重新读取原 run 失败'
      slot.error = toStructuredApiError(error)
      announcement.value = slot.error.message
    } finally {
      if (pollControllers[side] === controller) pollControllers[side] = null
    }
  }

  function finishSide(side: RunSide): void {
    if (side === 'A') {
      view.value = 'setup'
      announcement.value = 'Run A 已完成。复制 A 的场景与 seed，再选择另一策略运行 B。'
      return
    }
    view.value = comparisonInvariant.value.ok ? 'compare' : 'setup'
    announcement.value = comparisonInvariant.value.ok
      ? 'Run B 已完成，A/B 对比已就绪。'
      : comparisonInvariant.value.message
  }

  async function tryAttachActiveRun(
    slot: RunSlot,
    config: RunLaunchConfig,
    error: unknown,
    signal: AbortSignal,
  ): Promise<boolean> {
    const structured = toStructuredApiError(error)
    const activeRunId = structured.details?.active_run_id
    if (structured.code !== 'run_already_active' || typeof activeRunId !== 'string') return false
    const envelope = await api.getRun(activeRunId, signal)
    const active = envelope.run
    if (
      active.scenario_id !== config.scenario_id
      || active.seed !== config.seed
      || active.baseline_policy !== config.baseline_policy
      || (active.recording_source_run_id ?? null) !== (config.recording_source_run_id ?? null)
    ) {
      return false
    }
    slot.run = { ...active, event_count: envelope.event_count ?? active.event_count }
    slot.phase = 'pending'
    slot.stage = '已重新附着服务端 active run'
    slot.error = null
    announcement.value = `已恢复 Run ${slot.side} · ${activeRunId}，未重复创建实验。`
    await pollUntilComplete(slot, signal)
    return true
  }

  function abortSlot(side: RunSide): void {
    pollControllers[side]?.abort()
    pollControllers[side] = null
  }

  async function pollUntilComplete(slot: RunSlot, signal: AbortSignal): Promise<void> {
    if (!slot.run) throw new Error('启动响应缺少 run 元数据')
    const runId = slot.run.run_id

    for (let attempt = 0; attempt < MAX_POLL_ATTEMPTS; attempt += 1) {
      const [runEnvelope, events] = await Promise.all([
        api.getRun(runId, signal),
        api.getEvents(runId, signal),
      ])
      slot.run = { ...runEnvelope.run, event_count: runEnvelope.event_count ?? events.length }
      slot.events = events
      slot.stage = slot.run.ended_at ? '正在生成评估报告' : `场景运行中 · ${events.length} events`

      if (slot.run.ended_at) {
        const [report, finalEvents] = await Promise.all([
          api.getReport(runId, signal),
          api.getEvents(runId, signal),
        ])
        slot.report = report
        slot.events = finalEvents
        slot.run = { ...slot.run, event_count: finalEvents.length }
        slot.phase = 'success'
        slot.stage = report.outcome === 'pass' ? '评估通过' : '评估完成'
        slot.error = null
        return
      }
      await abortableDelay(POLL_INTERVAL_MS, signal)
    }
    throw new Error('等待 run 完成超时；运行仍可能在服务端继续，可稍后重试。')
  }

  function validateConfig(config: RunLaunchConfig, side: RunSide) {
    if (!config.scenario_id) {
      return { code: 'scenario_required', message: '请选择一个 canonical scenario。', status: null, details: null }
    }
    if (!Number.isSafeInteger(config.seed) || config.seed < 0) {
      return { code: 'invalid_seed', message: 'seed 必须是非负安全整数。', status: null, details: { seed: config.seed } }
    }
    if (side === 'B' && slotA.run && !launchMatchesRun(config, slotA.run)) {
      return {
        code: 'comparison_invariant',
        message: 'Run B 必须沿用 Run A 的 canonical scenario 与 seed；请先复制 A 参数。',
        status: null,
        details: {
          run_a_scenario_id: slotA.run.scenario_id,
          run_a_seed: slotA.run.seed,
          requested_scenario_id: config.scenario_id,
          requested_seed: config.seed,
        },
      }
    }
    if (isBusy.value) {
      return { code: 'run_in_progress', message: '当前 run 尚未完成，请等待后再启动下一次运行。', status: null, details: null }
    }
    return null
  }

  onScopeDispose(() => {
    scenariosResource.abort()
    runsResource.abort()
    abortSlot('A')
    abortSlot('B')
  })

  return {
    view,
    form,
    announcement,
    scenariosResource: scenariosResource.state,
    runsResource: runsResource.state,
    scenarios,
    recentRuns,
    selectedScenario,
    slotA,
    slotB,
    activeSide,
    isBusy,
    comparisonInvariant,
    initialize,
    retryScenarios,
    refreshRecentRuns,
    getRawTrace,
    selectScenario,
    copyAParameters,
    startSide,
    retrySide,
  }
}

function emptySlot(side: RunSide): RunSlot {
  return {
    side,
    phase: 'empty',
    stage: '尚未运行',
    config: null,
    run: null,
    report: null,
    events: [],
    error: null,
  }
}
