import type { SimEvent } from '@/types/sim-event'

export type RunEventDecision = 'accept' | 'switch' | 'ignore'

/**
 * Decide whether a live event belongs to the currently displayed run.
 * A reset event is the only event allowed to announce a different run before
 * SIMULATION_STATUS, because reset is deliberately broadcast first by backend.
 */
export function decideRunEvent(
  currentRunId: string | null,
  event: Pick<SimEvent, 'event_type' | 'run_id'>,
): RunEventDecision {
  const eventRunId = event.run_id ?? null
  if (eventRunId === null) return currentRunId === null ? 'accept' : 'ignore'
  if (currentRunId === null) return 'switch'
  if (eventRunId === currentRunId) return 'accept'
  return event.event_type === 'system.simulation_reset' ? 'switch' : 'ignore'
}
