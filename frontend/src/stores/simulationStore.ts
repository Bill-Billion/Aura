import { defineStore } from 'pinia'
import { ref } from 'vue'

export type ConnectionStatus = 'disconnected' | 'connecting' | 'connected' | 'error'

export const useSimulationStore = defineStore('simulation', () => {
  // --- State ---
  const isRunning = ref(false)
  const speed = ref(1)
  const connectionStatus = ref<ConnectionStatus>('disconnected')

  // --- Actions ---
  function setRunning(value: boolean) {
    isRunning.value = value
  }

  function setSpeed(value: number) {
    speed.value = value
  }

  function setConnectionStatus(status: ConnectionStatus) {
    connectionStatus.value = status
  }

  function $reset() {
    isRunning.value = false
    speed.value = 1
    connectionStatus.value = 'disconnected'
  }

  return {
    isRunning,
    speed,
    connectionStatus,
    setRunning,
    setSpeed,
    setConnectionStatus,
    $reset,
  }
})
