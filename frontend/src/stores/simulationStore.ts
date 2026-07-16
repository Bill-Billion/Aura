import { defineStore } from 'pinia'
import { ref } from 'vue'

export type ConnectionStatus = 'disconnected' | 'connecting' | 'connected' | 'error'

export const useSimulationStore = defineStore('simulation', () => {
  // --- State ---
  const isRunning = ref(false)
  const speed = ref(1)
  const mode = ref<'observe' | 'demo'>('observe')
  const wallTickMs = ref(2000)
  const simulatedDtSeconds = ref(10)
  const connectionStatus = ref<ConnectionStatus>('disconnected')

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

  function $reset() {
    isRunning.value = false
    speed.value = 1
    mode.value = 'observe'
    wallTickMs.value = 2000
    simulatedDtSeconds.value = 10
    connectionStatus.value = 'disconnected'
  }

  return {
    isRunning,
    speed,
    mode,
    wallTickMs,
    simulatedDtSeconds,
    connectionStatus,
    setRunning,
    setSpeed,
    setMode,
    setWallTickMs,
    setSimulatedDtSeconds,
    setConnectionStatus,
    $reset,
  }
})
