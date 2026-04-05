import { defineStore } from 'pinia'
import { reactive, ref } from 'vue'
import type {
  DeviceState,
  RoomState,
  EnvironmentState,
  WorldStateSnapshot,
  DeltaChange,
} from '@/types/world-state'

export const useWorldStore = defineStore('world', () => {
  // --- State ---
  const simulationTick = ref(0)
  const simulationSpeed = ref(1)
  const isRunning = ref(false)
  const sceneId = ref('')

  const environment = reactive<EnvironmentState>({
    time_of_day: '08:00',
    outdoor_temp: 22,
    outdoor_humidity: 50,
    weather: 'clear',
  })

  const devices = reactive<Record<string, DeviceState>>({})
  const rooms = reactive<Record<string, RoomState>>({})

  // --- Actions ---

  /** Populate from STATE_FULL message */
  function applyFullState(snap: WorldStateSnapshot) {
    simulationTick.value = snap.simulation_tick
    simulationSpeed.value = snap.simulation_speed
    isRunning.value = snap.is_running
    sceneId.value = snap.scene_id

    Object.assign(environment, snap.environment)

    // Clear and refill devices
    for (const key of Object.keys(devices)) {
      delete devices[key]
    }
    for (const [id, dev] of Object.entries(snap.devices)) {
      devices[id] = { ...dev }
    }

    // Clear and refill rooms
    for (const key of Object.keys(rooms)) {
      delete rooms[key]
    }
    for (const [id, room] of Object.entries(snap.rooms)) {
      rooms[id] = { ...room }
    }
  }

  /** Parse and apply a list of delta changes */
  function applyDelta(changes: DeltaChange[]) {
    for (const change of changes) {
      applySingleDelta(change)
    }
  }

  function applySingleDelta(change: DeltaChange) {
    const path = change.path
    const value = change.new_value

    // Parse path like "devices[light_01].state.extra.brightness"
    // or "rooms[living_room].temperature"
    // or "environment.outdoor_temp"
    // or "simulation_tick"

    const directKeys = ['simulation_tick', 'simulation_speed', 'is_running', 'scene_id']
    if (directKeys.includes(path)) {
      switch (path) {
        case 'simulation_tick': simulationTick.value = value; break
        case 'simulation_speed': simulationSpeed.value = value; break
        case 'is_running': isRunning.value = value; break
        case 'scene_id': sceneId.value = value; break
      }
      return
    }

    // Parse "root[key].sub.path" pattern
    const rootMatch = path.match(/^(\w+)\[([^\]]+)\]\.(.+)$/)
    if (rootMatch) {
      const [, root, key, subPath] = rootMatch
      const container = root === 'devices' ? devices : root === 'rooms' ? rooms : null
      if (container && container[key]) {
        setNestedValue(container[key], subPath, value)
      }
      return
    }

    // Parse "environment.field" pattern
    if (path.startsWith('environment.')) {
      const field = path.replace('environment.', '')
      if (field in environment) {
        ;(environment as any)[field] = value
      }
      return
    }
  }

  /** Set a value at a dot-separated path on an object, reactively */
  function setNestedValue(obj: any, path: string, value: any) {
    const parts = path.split('.')
    let current = obj
    for (let i = 0; i < parts.length - 1; i++) {
      if (current[parts[i]] === undefined) {
        current[parts[i]] = {}
      }
      current = current[parts[i]]
    }
    current[parts[parts.length - 1]] = value
  }

  return {
    simulationTick,
    simulationSpeed,
    isRunning,
    sceneId,
    environment,
    devices,
    rooms,
    applyFullState,
    applyDelta,
  }
})
