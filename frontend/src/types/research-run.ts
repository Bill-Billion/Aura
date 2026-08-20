import type { EvalReport, RunSummary } from './eval-report'
import type { SimEvent } from './sim-event'

export type BaselinePolicy =
  | 'rule_based'
  | 'llm_mocked'
  | 'llm_recorded'
  | 'llm_live'

export type EffectiveLLMMode = 'rule_based' | 'mocked' | 'recorded' | 'live'
export type RunSide = 'A' | 'B'
export type WorkspaceView = 'setup' | 'live' | 'compare'
export type RunSlotPhase = 'empty' | 'loading' | 'pending' | 'success' | 'error'
export type RemoteStatus = 'idle' | 'loading' | 'success' | 'error'

export interface ScenarioSummary {
  id: string
  name: string
  description: string
  seed: number
  mode: 'observe' | 'demo' | 'stress'
  duration_seconds: number | null
  involved_agents: string[]
  timeline_event_count: number
  expected_device_effect_count: number
  root_event_types: string[]
  has_ground_truth: boolean
  scenario_schema_version: string
}

export interface RunLaunchConfig {
  scenario_id: string
  seed: number
  baseline_policy: BaselinePolicy
  idempotency_key: string
  recording_source_run_id?: string
}

export interface StructuredApiError {
  code: string
  message: string
  status: number | null
  details: Record<string, unknown> | null
}

export type RawRunEvent = SimEvent & {
  seq: number
  run_id?: string | null
  scenario_id?: string | null
  depth?: number
  event_generation_mode?: string | null
  generation_rule_id?: string | null
  rng_stream?: string | null
  sim_time_s?: number
}

export interface RunSlot {
  side: RunSide
  phase: RunSlotPhase
  stage: string
  config: RunLaunchConfig | null
  run: RunSummary | null
  report: EvalReport | null
  events: RawRunEvent[]
  error: StructuredApiError | null
}

export interface RemoteState<T> {
  status: RemoteStatus
  data: T | null
  error: StructuredApiError | null
}

export interface RunEventsEnvelope {
  run_id: string
  count: number
  total: number
  offset: number
  events: RawRunEvent[]
}

export interface RunEnvelope {
  run: RunSummary
  event_count?: number
  artifacts?: string[]
}

export interface ScenarioListEnvelope {
  count: number
  scenarios: ScenarioSummary[]
}

export interface RunListEnvelope {
  count: number
  runs: RunSummary[]
}

export interface StartRunEnvelope {
  run: RunSummary
}
