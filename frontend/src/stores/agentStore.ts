import { defineStore } from 'pinia'
import { reactive, ref } from 'vue'
import type { AgentState } from '@/types/world-state'

export interface AgentLogEntry {
  timestamp: number
  agent_name: string
  action: string
  reason: string
}

const MAX_LOG_ENTRIES = 100

export const useAgentStore = defineStore('agent', () => {
  // --- State ---
  const agents = reactive<Record<string, AgentState>>({})
  const actionLog = ref<AgentLogEntry[]>([])

  // --- Actions ---

  /** Update a single agent's status */
  function updateStatus(agentId: string, data: Partial<AgentState>) {
    if (!agents[agentId]) {
      agents[agentId] = {
        id: agentId,
        name: data.name ?? agentId,
        status: data.status ?? 'idle',
        current_strategy: data.current_strategy ?? '',
        confidence: data.confidence ?? 0,
        last_action: data.last_action ?? '',
      }
    } else {
      Object.assign(agents[agentId], data)
    }
  }

  /** Replace all agents (e.g. from STATE_FULL) */
  function setAllAgents(agentMap: Record<string, AgentState>) {
    for (const key of Object.keys(agents)) {
      delete agents[key]
    }
    for (const [id, agent] of Object.entries(agentMap)) {
      agents[id] = { ...agent }
    }
  }

  /** Append an entry to the action log, trimming to MAX_LOG_ENTRIES */
  function appendLog(entry: AgentLogEntry) {
    actionLog.value.push(entry)
    if (actionLog.value.length > MAX_LOG_ENTRIES) {
      actionLog.value = actionLog.value.slice(-MAX_LOG_ENTRIES)
    }
  }

  /** Clear all state */
  function $reset() {
    for (const key of Object.keys(agents)) {
      delete agents[key]
    }
    actionLog.value = []
  }

  return {
    agents,
    actionLog,
    updateStatus,
    setAllAgents,
    appendLog,
    $reset,
  }
})
