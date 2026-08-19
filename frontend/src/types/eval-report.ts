/** S4 评估报告的前端投影。字段名与 spec §12 的七项 canonical metrics 对齐。 */

export type CanonicalMetricKey =
  | 'episode_complete'
  | 'first_action_latency_ms'
  | 'command_failure_count'
  | 'fallback_count'
  | 'conflict_count'
  | 'user_intent_satisfied'
  | 'device_state_match_rate'

export interface MetricDatum {
  name: string
  value: number | boolean | null
  unit: string
  details: Record<string, unknown>
}

export type EvalMetrics = Record<CanonicalMetricKey, MetricDatum>

export interface EvalReportProvenance extends Record<string, unknown> {
  scenario_contract_hash?: string | null
  source_revision?: string | null
  evaluator_source_revision?: string | null
}

export interface EvalReport {
  report_schema_version?: string
  run_id: string
  scenario_id: string | null
  seed: number | null
  outcome: 'pass' | 'fail' | 'error'
  metrics: EvalMetrics
  criteria_checks: Record<string, boolean>
  failed_metrics?: string[]
  failure_reasons: string[]
  provenance?: EvalReportProvenance
  metadata: Record<string, unknown>
  scenario?: Record<string, unknown> | null
}

export interface RunSummary {
  run_id: string
  scenario_id: string | null
  seed: number | null
  started_at: string | null
  ended_at: string | null
  end_reason: string | null
  event_count: number | null
  baseline_policy?: string | null
  recording_source_run_id?: string | null
  llm_mode: string
  llm_provider: string
  llm_model: string
  sim_version: string
  source_revision: string
  agent_versions?: Record<string, string>
  initial_state_hash: string
  scenario_contract_hash?: string | null
  event_schema_version?: string
  scenario_schema_version?: string
  command_schema_version?: string
  device_registry_version?: string
  [key: string]: unknown
}
