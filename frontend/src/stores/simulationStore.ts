import { defineStore } from 'pinia'
import { ref } from 'vue'
import type { BaselinePolicy, EffectiveLLMMode } from '@/types/research-run'
import type { ErrorPayload, SimulationStatusPayload } from '@/types/websocket'

export type ConnectionStatus = 'disconnected' | 'connecting' | 'connected' | 'error'

export const useSimulationStore = defineStore('simulation', () => {
  // --- State ---
  const isRunning = ref(false)
  const speed = ref(1)
  const mode = ref<'observe' | 'demo'>('observe')
  const wallTickMs = ref(2000)
  const simulatedDtSeconds = ref(10)
  const connectionStatus = ref<ConnectionStatus>('disconnected')
  const currentRunId = ref<string | null>(null)
  const currentScenarioId = ref<string | null>(null)
  const currentSeed = ref<number | null>(null)
  const currentBaselinePolicy = ref<BaselinePolicy | null>(null)
  const currentLlmMode = ref<EffectiveLLMMode | null>(null)
  const currentDurationSeconds = ref<number | null>(null)
  const currentRecordingSourceRunId = ref<string | null>(null)
  const currentRunFinalized = ref<boolean | null>(null)
  const currentRunEndedAt = ref<string | null>(null)
  const currentRunEndReason = ref<string | null>(null)
  const lastCommandError = ref<ErrorPayload | null>(null)

  // --- Actions ---
  function setRunning(value: boolean) {
    isRunning.value = value
  }

  function setSpeed(value: number) {
    speed.value = value
  }

  function setMode(value: 'observe' | 'demo') {
    mode.value = value
  }

  function setWallTickMs(value: number) {
    wallTickMs.value = value
  }

  function setSimulatedDtSeconds(value: number) {
    simulatedDtSeconds.value = value
  }

  function setConnectionStatus(status: ConnectionStatus) {
    connectionStatus.value = status
  }

  function applySimulationStatus(status: SimulationStatusPayload): boolean {
    let runChanged = false
    if (Object.hasOwn(status, 'run_id')) {
      const nextRunId = status.run_id ?? null
      runChanged = nextRunId !== currentRunId.value
      if (runChanged) resetRunIdentity(nextRunId)
    }
    if (Object.hasOwn(status, 'scenario_id')) currentScenarioId.value = status.scenario_id ?? null
    if (Object.hasOwn(status, 'seed')) currentSeed.value = status.seed ?? null
    if (Object.hasOwn(status, 'baseline_policy')) currentBaselinePolicy.value = status.baseline_policy ?? null
    if (Object.hasOwn(status, 'llm_mode')) currentLlmMode.value = status.llm_mode ?? null
    if (Object.hasOwn(status, 'duration_seconds')) currentDurationSeconds.value = status.duration_seconds ?? null
    if (Object.hasOwn(status, 'recording_source_run_id')) {
      currentRecordingSourceRunId.value = status.recording_source_run_id ?? null
    }
    if (Object.hasOwn(status, 'finalized')) currentRunFinalized.value = status.finalized ?? null
    if (Object.hasOwn(status, 'ended_at')) currentRunEndedAt.value = status.ended_at ?? null
    if (Object.hasOwn(status, 'end_reason')) currentRunEndReason.value = status.end_reason ?? null
    return runChanged
  }

  function adoptRunFromEvent(runId: string, scenarioId: string | null = null): boolean {
    if (runId === currentRunId.value) return false
    resetRunIdentity(runId)
    currentScenarioId.value = scenarioId
    currentRunFinalized.value = false
    return true
  }

  function clearRunIdentity() {
    resetRunIdentity(null)
  }

  function setCommandError(error: ErrorPayload) {
    lastCommandError.value = {
      code: error.code,
      message: error.message,
      details: error.details ? { ...error.details } : null,
    }
  }

  function clearCommandError() {
    lastCommandError.value = null
  }

  function resetRunIdentity(runId: string | null) {
    currentRunId.value = runId
    currentScenarioId.value = null
    currentSeed.value = null
    currentBaselinePolicy.value = null
    currentLlmMode.value = null
    currentDurationSeconds.value = null
    currentRecordingSourceRunId.value = null
    currentRunFinalized.value = null
    currentRunEndedAt.value = null
    currentRunEndReason.value = null
  }

  function $reset() {
    isRunning.value = false
    speed.value = 1
    mode.value = 'observe'
    wallTickMs.value = 2000
    simulatedDtSeconds.value = 10
    connectionStatus.value = 'disconnected'
    resetRunIdentity(null)
    lastCommandError.value = null
  }

  return {
    isRunning,
    speed,
    mode,
    wallTickMs,
    simulatedDtSeconds,
    connectionStatus,
    currentRunId,
    currentScenarioId,
    currentSeed,
    currentBaselinePolicy,
    currentLlmMode,
    currentDurationSeconds,
    currentRecordingSourceRunId,
    currentRunFinalized,
    currentRunEndedAt,
    currentRunEndReason,
    lastCommandError,
    setRunning,
    setSpeed,
    setMode,
    setWallTickMs,
    setSimulatedDtSeconds,
    setConnectionStatus,
    applySimulationStatus,
    adoptRunFromEvent,
    clearRunIdentity,
    setCommandError,
    clearCommandError,
    $reset,
  }
})
