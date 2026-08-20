import { defineStore } from 'pinia'
import { computed, ref, watch } from 'vue'
import { useAgentStore } from '@/stores/agentStore'
import { useSimulationStore } from '@/stores/simulationStore'
import { useUIStore } from '@/stores/uiStore'
import type {
  ConnectionStateDerived,
  EventFilters,
  EpisodeSummary,
  SimEvent,
} from '@/types/sim-event'
import {
  buildEpisodeNodes,
  buildEpisodeSummaries,
  buildEventDetailView,
  createDefaultEventFilters,
  deriveObservabilityState,
  filterEpisodes,
  pickDefaultEpisode,
} from '@/utils/observability'
import { decideRunEvent } from '@/utils/runEventIsolation'

const MAX_EVENTS = 500

export const useEventStore = defineStore('events', () => {
  const agentStore = useAgentStore()
  const simulationStore = useSimulationStore()
  const uiStore = useUIStore()
  const events = ref<SimEvent[]>([])
  const selectedEpisodeId = ref<string | null>(null)
  const selectedEventId = ref<string | null>(null)
  const filters = ref<EventFilters>(createDefaultEventFilters())
  const selectionPinned = ref(false)
  const eventRunId = ref<string | null>(null)

  // S5-T5：3D 设备点击 → 自动设置 deviceId 过滤
  watch(
    () => uiStore.activeDevice,
    (deviceId) => {
      filters.value = { ...filters.value, deviceId }
    },
  )

  const allEpisodes = computed(() => buildEpisodeSummaries(events.value, agentStore.agents))
  const episodes = computed(() => filterEpisodes(allEpisodes.value, filters.value))

  const selectedEpisode = computed<EpisodeSummary | null>(() => {
    if (!selectedEpisodeId.value) {
      return null
    }
    return episodes.value.find((episode) => episode.correlationId === selectedEpisodeId.value) ?? null
  })

  const selectedEvent = computed<SimEvent | null>(() => {
    if (!selectedEventId.value || !selectedEpisode.value) {
      return null
    }
    return selectedEpisode.value.events.find((event) => event.event_id === selectedEventId.value) ?? null
  })

  const selectedEpisodeNodes = computed(() => (
    selectedEpisode.value ? buildEpisodeNodes(selectedEpisode.value) : []
  ))

  const selectedEventDetail = computed(() => (
    selectedEvent.value ? buildEventDetailView(selectedEvent.value) : null
  ))

  const connectionStateDerived = computed<ConnectionStateDerived>(() => {
    return simulationStore.connectionStatus === 'connected' || simulationStore.connectionStatus === 'connecting'
      ? simulationStore.connectionStatus
      : 'disconnected'
  })

  const panelState = computed(() => deriveObservabilityState({
    connectionState: connectionStateDerived.value,
    isRunning: simulationStore.isRunning,
    selectedEpisode: selectedEpisode.value,
    selectedEvent: selectedEvent.value,
  }))

  watch(episodes, () => {
    if (!selectionPinned.value) {
      syncSelection()
    }
  })

  function appendEvent(event: SimEvent): boolean {
    const decision = decideRunEvent(simulationStore.currentRunId, event)
    if (decision === 'ignore') return false
    if (decision === 'switch') {
      const nextRunId = event.run_id ?? null
      synchronizeRun(nextRunId)
      if (nextRunId) simulationStore.adoptRunFromEvent(nextRunId, event.scenario_id ?? null)
    } else if (eventRunId.value !== simulationStore.currentRunId) {
      synchronizeRun(simulationStore.currentRunId)
    }
    events.value.push(event)
    if (events.value.length > MAX_EVENTS) {
      events.value = events.value.slice(-MAX_EVENTS)
    }
    syncSelection()
    return true
  }

  function selectEpisode(correlationId: string | null) {
    selectionPinned.value = correlationId !== null
    if (!correlationId) {
      syncSelection(true)
      return
    }

    const episode = episodes.value.find((entry) => entry.correlationId === correlationId) ?? null
    selectedEpisodeId.value = episode?.correlationId ?? null
    selectedEventId.value = episode?.rootEventId ?? null
  }

  function selectEvent(eventId: string | null) {
    if (!eventId || !selectedEpisode.value) {
      selectedEventId.value = null
      return
    }

    const exists = selectedEpisode.value.events.some((event) => event.event_id === eventId)
    if (exists) {
      selectionPinned.value = true
      selectedEventId.value = eventId
    }
  }

  function setFilters(nextFilters: Partial<EventFilters>) {
    filters.value = {
      ...filters.value,
      ...nextFilters,
    }
    syncSelection(true)
  }

  function clear() {
    clearRunEvents()
    filters.value = createDefaultEventFilters()
    eventRunId.value = null
  }

  function clearRunEvents() {
    events.value = []
    selectedEpisodeId.value = null
    selectedEventId.value = null
    selectionPinned.value = false
  }

  function synchronizeRun(runId: string | null, forceClear = false) {
    if (!forceClear && eventRunId.value === runId) return
    clearRunEvents()
    eventRunId.value = runId
  }

  function syncSelection(forceDefault = false) {
    const episodeList = episodes.value
    const defaultEpisode = pickDefaultEpisode(episodeList)
    const currentEpisode = selectedEpisodeId.value
      ? episodeList.find((episode) => episode.correlationId === selectedEpisodeId.value) ?? null
      : null

    const shouldFollowDefault = forceDefault || !selectionPinned.value || !currentEpisode
    const targetEpisode = shouldFollowDefault ? defaultEpisode : currentEpisode

    selectedEpisodeId.value = targetEpisode?.correlationId ?? null

    if (!targetEpisode) {
      selectedEventId.value = null
      selectionPinned.value = false
      return
    }

    const hasSelectedEvent = selectedEventId.value
      ? targetEpisode.events.some((event) => event.event_id === selectedEventId.value)
      : false

    if (forceDefault || !hasSelectedEvent) {
      selectedEventId.value = targetEpisode.rootEventId
    }

    if (shouldFollowDefault) {
      selectionPinned.value = false
    }
  }

  return {
    events,
    eventRunId,
    filters,
    episodes,
    selectedEpisodeId,
    selectedEventId,
    selectedEpisode,
    selectedEvent,
    selectedEpisodeNodes,
    selectedEventDetail,
    connectionStateDerived,
    panelState,
    appendEvent,
    selectEpisode,
    selectEvent,
    setFilters,
    synchronizeRun,
    clear,
  }
})
