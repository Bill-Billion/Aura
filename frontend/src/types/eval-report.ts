/** S4 评估报告的类型——S5 对比视图消费。与 backend/evaluation/evaluator.py::EvalReport 对应。 */

export interface MetricDatum {
  name: string
  value: number
  unit: string
  details: Record<string, unknown>
}

export interface EvalMetrics {
  episode_completeness: MetricDatum
  first_action_latency_ms: MetricDatum
  command_success_rate: MetricDatum
  fallback_rate: MetricDatum
  coordination_effectiveness: MetricDatum
  safety_compliance: MetricDatum
  device_effect_accuracy: MetricDatum
}

export interface EvalReport {
  run_id: string
  scenario_id: string | null
  seed: number | null
  outcome: 'pass' | 'fail' | 'error'
  metrics: EvalMetrics
  criteria_checks: Record<string, boolean>
  failure_reasons: string[]
  metadata: Record<string, unknown>
}

export interface RunSummary {
  run_id: string
  scenario_id: string | null
  seed: number | null
  started_at: string | null
  ended_at: string | null
  end_reason: string | null
  event_count: number | null
  llm_mode: string
  llm_provider: string
  llm_model: string
  sim_version: string
  initial_state_hash: string
}
