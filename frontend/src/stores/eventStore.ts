import { defineStore } from 'pinia'
import { computed, ref, watch } from 'vue'
import { useAgentStore } from '@/stores/agentStore'
import { useSimulationStore } from '@/stores/simulationStore'
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

const MAX_EVENTS = 500

export const useEventStore = defineStore('events', () => {
  const agentStore = useAgentStore()
  const simulationStore = useSimulationStore()
  const events = ref<SimEvent[]>([])
  const selectedEpisodeId = ref<string | null>(null)
  const selectedEventId = ref<string | null>(null)
  const filters = ref<EventFilters>(createDefaultEventFilters())
  const selectionPinned = ref(false)

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

  function appendEvent(event: SimEvent) {
    events.value.push(event)
    if (events.value.length > MAX_EVENTS) {
      events.value = events.value.slice(-MAX_EVENTS)
    }
    syncSelection()
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
    events.value = []
    selectedEpisodeId.value = null
    selectedEventId.value = null
    filters.value = createDefaultEventFilters()
    selectionPinned.value = false
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
    clear,
  }
})
