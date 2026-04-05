import { ref, readonly } from 'vue'
import { useWorldStore } from '@/stores/worldStore'
import { useAgentStore } from '@/stores/agentStore'
import { useSimulationStore } from '@/stores/simulationStore'
import type { WSMessage } from '@/types/websocket'
import type { WorldStateSnapshot, DeltaChange } from '@/types/world-state'

type ConnectionStatus = 'disconnected' | 'connecting' | 'connected' | 'error'

// Reconnect config
const INITIAL_RECONNECT_DELAY = 1000
const MAX_RECONNECT_DELAY = 30000
const BACKOFF_MULTIPLIER = 2

let ws: WebSocket | null = null
let reconnectTimer: ReturnType<typeof setTimeout> | null = null
let reconnectDelay = INITIAL_RECONNECT_DELAY
let intentionalClose = false

const connectionStatus = ref<ConnectionStatus>('disconnected')

export function useWebSocket() {
  const worldStore = useWorldStore()
  const agentStore = useAgentStore()
  const simulationStore = useSimulationStore()

  // ---- Connect ----

  function connect(url: string) {
    if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) {
      return // already connected or connecting
    }

    intentionalClose = false
    simulationStore.setConnectionStatus('connecting')
    connectionStatus.value = 'connecting'

    ws = new WebSocket(url)

    ws.onopen = () => {
      reconnectDelay = INITIAL_RECONNECT_DELAY
      simulationStore.setConnectionStatus('connected')
      connectionStatus.value = 'connected'
    }

    ws.onmessage = (event: MessageEvent) => {
      try {
        const msg: WSMessage = JSON.parse(event.data as string)
        handleMessage(msg)
      } catch {
        console.warn('[WebSocket] Failed to parse message:', event.data)
      }
    }

    ws.onclose = () => {
      simulationStore.setConnectionStatus('disconnected')
      connectionStatus.value = 'disconnected'

      if (!intentionalClose) {
        scheduleReconnect(url)
      }
    }

    ws.onerror = () => {
      simulationStore.setConnectionStatus('error')
      connectionStatus.value = 'error'
    }
  }

  // ---- Disconnect ----

  function disconnect() {
    intentionalClose = true
    clearReconnectTimer()
    if (ws) {
      ws.close()
      ws = null
    }
    simulationStore.setConnectionStatus('disconnected')
    connectionStatus.value = 'disconnected'
  }

  // ---- Send ----

  function sendCommand(type: string, payload: Record<string, any> = {}) {
    if (ws && ws.readyState === WebSocket.OPEN) {
      const msg: WSMessage = {
        type,
        payload,
        timestamp: Date.now(),
      }
      ws.send(JSON.stringify(msg))
    } else {
      console.warn('[WebSocket] Cannot send, socket not open')
    }
  }

  // ---- Message routing ----

  function handleMessage(msg: WSMessage) {
    switch (msg.type) {
      case 'STATE_FULL': {
        const snap = msg.payload as WorldStateSnapshot
        worldStore.applyFullState(snap)
        agentStore.setAllAgents(snap.agents)
        simulationStore.setRunning(snap.is_running)
        simulationStore.setSpeed(snap.simulation_speed)
        break
      }

      case 'STATE_DELTA': {
        const deltas = (msg.payload?.deltas ?? []) as DeltaChange[]
        worldStore.applyDelta(deltas)

        // Extract agent actions from delta metadata for the log
        for (const delta of deltas) {
          if (delta.caused_by) {
            agentStore.appendLog({
              timestamp: Date.now(),
              agent_name: delta.caused_by,
              action: `Changed ${delta.path}`,
              reason: delta.reason ?? '',
            })
          }
        }
        break
      }

      case 'AGENT_STATUS': {
        const agents = msg.payload?.agents as Record<string, any> | undefined
        if (agents) {
          for (const [id, data] of Object.entries(agents)) {
            agentStore.updateStatus(id, data)
          }
        }
        break
      }

      case 'EVENT_NOTIFICATION': {
        // Could trigger a toast / notification in the UI
        console.info('[WebSocket] Event:', msg.payload)
        break
      }

      case 'SIMULATION_STATUS': {
        const p = msg.payload
        if (typeof p?.is_running === 'boolean') {
          simulationStore.setRunning(p.is_running)
          worldStore.isRunning = p.is_running
        }
        if (typeof p?.speed === 'number') {
          simulationStore.setSpeed(p.speed)
          worldStore.simulationSpeed = p.speed
        }
        break
      }

      case 'HEARTBEAT_PING': {
        // Auto-reply with pong
        sendCommand('HEARTBEAT_PONG', { timestamp: Date.now() })
        break
      }

      default:
        // Silently ignore unknown message types
        break
    }
  }

  // ---- Auto-reconnect ----

  function scheduleReconnect(url: string) {
    clearReconnectTimer()
    reconnectTimer = setTimeout(() => {
      console.info(`[WebSocket] Reconnecting in ${reconnectDelay}ms...`)
      connect(url)
    }, reconnectDelay)

    reconnectDelay = Math.min(reconnectDelay * BACKOFF_MULTIPLIER, MAX_RECONNECT_DELAY)
  }

  function clearReconnectTimer() {
    if (reconnectTimer !== null) {
      clearTimeout(reconnectTimer)
      reconnectTimer = null
    }
  }

  return {
    connectionStatus: readonly(connectionStatus),
    connect,
    disconnect,
    sendCommand,
  }
}
