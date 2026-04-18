export interface SimEvent {
  event_id: string
  event_type: string
  source: string
  timestamp: number
  wall_time: number
  correlation_id: string
  causal_parent: string | null
  priority: number
  data: Record<string, unknown>
}
