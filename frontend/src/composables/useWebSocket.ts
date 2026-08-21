import { ref, readonly } from 'vue'
import { useWorldStore } from '@/stores/worldStore'
import { useAgentStore } from '@/stores/agentStore'
import { useSimulationStore } from '@/stores/simulationStore'
import { useEventStore } from '@/stores/eventStore'
import type {
  AgentStatusPayload,
  ErrorPayload,
  SimulationStatusPayload,
  WSMessage,
} from '@/types/websocket'
import type { WorldStateSnapshot, DeltaChange } from '@/types/world-state'
import type { SimEvent } from '@/types/sim-event'

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
  const eventStore = useEventStore()

  // ---- Connect ----

  function connect(url: string) {
    if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) {
      return // already connected or connecting
    }

    // A new socket has not proved which run owns its events yet. Drop the
    // previous identity before waiting for STATE_FULL + SIMULATION_STATUS.
    simulationStore.clearRunIdentity()
    eventStore.synchronizeRun(null, true)
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
      simulationStore.clearRunIdentity()
      eventStore.synchronizeRun(null, true)

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
    simulationStore.clearRunIdentity()
    eventStore.synchronizeRun(null, true)
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
        simulationStore.clearCommandError()
        worldStore.applyFullState(snap)
        agentStore.setAllAgents(snap.agents)
        simulationStore.setRunning(snap.is_running)
        simulationStore.setSpeed(snap.simulation_speed)
        simulationStore.setMode(snap.simulation_mode)
        simulationStore.setWallTickMs(snap.wall_tick_ms)
        simulationStore.setSimulatedDtSeconds(snap.simulated_dt_seconds)
        break
      }

      case 'STATE_DELTA': {
        const deltas = ((msg.payload as { deltas?: DeltaChange[] } | undefined)?.deltas ?? []) as DeltaChange[]
        worldStore.applyDelta(deltas)
        break
      }

      case 'AGENT_STATUS': {
        const agents = (msg.payload as AgentStatusPayload | undefined)?.agents
        if (agents) {
          for (const [id, data] of Object.entries(agents)) {
            agentStore.updateStatus(id, data)
          }
        }
        break
      }

      case 'EVENT_NOTIFICATION': {
        console.info('[WebSocket] Event:', msg.payload)
        break
      }

      case 'SIM_EVENT': {
        eventStore.appendEvent(msg.payload as SimEvent)
        break
      }

      case 'SIMULATION_STATUS': {
        const p = msg.payload as SimulationStatusPayload
        simulationStore.clearCommandError()
        simulationStore.applySimulationStatus(p)
        eventStore.synchronizeRun(simulationStore.currentRunId)
        if (typeof p?.is_running === 'boolean') {
          simulationStore.setRunning(p.is_running)
          worldStore.isRunning = p.is_running
        }
        if (typeof p?.speed === 'number') {
          simulationStore.setSpeed(p.speed)
          worldStore.simulationSpeed = p.speed
        }
        if (p?.mode === 'observe' || p?.mode === 'demo') {
          simulationStore.setMode(p.mode)
          worldStore.simulationMode = p.mode
        }
        if (typeof p?.wall_tick_ms === 'number') {
          simulationStore.setWallTickMs(p.wall_tick_ms)
          worldStore.wallTickMs = p.wall_tick_ms
        }
        if (typeof p?.simulated_dt_seconds === 'number') {
          simulationStore.setSimulatedDtSeconds(p.simulated_dt_seconds)
          worldStore.simulatedDtSeconds = p.simulated_dt_seconds
        }
        break
      }

      case 'ERROR': {
        const error = msg.payload as ErrorPayload
        simulationStore.setCommandError(error)
        console.warn('[WebSocket] Command rejected:', {
          code: error.code,
          message: error.message,
          details: error.details ?? null,
        })
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
