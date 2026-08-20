<script setup lang="ts">
import { computed } from 'vue'
import { storeToRefs } from 'pinia'
import { useEventStore } from '@/stores/eventStore'
import { useAgentStore } from '@/stores/agentStore'
import { useSimulationStore } from '@/stores/simulationStore'
import { useUIStore } from '@/stores/uiStore'
import { useWebSocket } from '@/composables/useWebSocket'
import ObservabilityHeader from './observability/ObservabilityHeader.vue'
import EventTimeline from './observability/EventTimeline.vue'
import ReasoningDetail from './observability/ReasoningDetail.vue'

withDefaults(defineProps<{
  embedded?: boolean
}>(), {
  embedded: false,
})

const eventStore = useEventStore()
const agentStore = useAgentStore()
const simulationStore = useSimulationStore()
const uiStore = useUIStore()
const { connect } = useWebSocket()

const {
  connectionStateDerived,
  episodes,
  filters,
  panelState,
  selectedEpisode,
  selectedEvent,
  selectedEventDetail,
  selectedEventId,
  selectedEpisodeNodes,
} = storeToRefs(eventStore)

const visibleEpisodes = computed(() => episodes.value.slice(0, 20))
const currentRunLabel = computed(() => {
  if (!simulationStore.currentRunId) return '未同步 run 身份'
  const id = simulationStore.currentRunId
  const shortId = id.length > 20 ? `${id.slice(0, 12)}…${id.slice(-6)}` : id
  return `${simulationStore.currentRunFinalized ? '已归档' : '当前'} ${shortId}`
})

const agentOptions = computed(() => {
  const fromAgents = Object.values(agentStore.agents).map((agent) => ({
    id: agent.id,
    label: agent.name || agent.id,
  }))

  const knownIds = new Set(fromAgents.map((agent) => agent.id))
  const fromEpisodes: Array<{ id: string; label: string }> = []
  for (const episode of visibleEpisodes.value) {
    for (const agentId of episode.agentIds) {
      if (knownIds.has(agentId)) continue
      knownIds.add(agentId)
      fromEpisodes.push({ id: agentId, label: agentId })
    }
  }

  return [...fromAgents, ...fromEpisodes]
})

function retryConnection() {
  const proto = window.location.protocol === 'https:' ? 'wss' : 'ws'
  connect(`${proto}://${window.location.host}/ws/simulation`)
}
</script>

<template>
  <section class="observability-panel" :class="{ 'is-embedded': embedded }">
    <ObservabilityHeader
      :connection-state="connectionStateDerived"
      :current-episode="selectedEpisode"
      :episodes="visibleEpisodes"
      :filters="filters"
      :agent-options="agentOptions"
      :show-close="!embedded"
      @close="uiStore.sidebarOpen = false"
      @retry="retryConnection"
      @select-episode="eventStore.selectEpisode"
      @update-filters="eventStore.setFilters"
    />

    <div class="observability-panel__body">
      <EventTimeline
        :nodes="selectedEpisodeNodes"
        :selected-event-id="selectedEventId"
        :panel-state="panelState"
        @select-event="eventStore.selectEvent"
      />

      <ReasoningDetail
        :detail="selectedEventDetail"
        :panel-state="panelState"
      />
    </div>

    <div class="observability-panel__footer">
      <span class="footer-chip">{{ currentRunLabel }}</span>
      <span class="footer-chip">
        {{ selectedEpisode ? `链路 ${selectedEpisode.correlationId}` : '等待链路' }}
      </span>
      <span class="footer-chip">
        {{ selectedEvent ? `事件 ${selectedEvent.event_id}` : '未选中事件' }}
      </span>
      <span class="footer-chip">
        {{ simulationStore.isRunning ? '模拟运行中' : '模拟未运行' }}
      </span>
    </div>
  </section>
</template>

<style scoped>
.observability-panel {
  display: flex;
  flex-direction: column;
  height: 100%;
  background: rgba(7, 9, 13, 0.98);
  border-left: 1px solid rgba(255, 255, 255, 0.08);
  box-shadow: -24px 0 40px rgba(0, 0, 0, 0.28);
}

.observability-panel.is-embedded {
  border: 1px solid rgba(255, 255, 255, 0.08);
  border-radius: 6px;
  box-shadow: none;
  overflow: hidden;
}

.observability-panel.is-embedded :deep(button),
.observability-panel.is-embedded :deep(select),
.observability-panel.is-embedded :deep(.fallback-toggle) {
  min-height: 40px;
}

.observability-panel.is-embedded :deep(button:focus-visible),
.observability-panel.is-embedded :deep(select:focus-visible),
.observability-panel.is-embedded :deep(input:focus-visible) {
  outline: 2px solid var(--color-primary);
  outline-offset: 2px;
}

.observability-panel__body {
  min-height: 0;
  flex: 1;
  display: grid;
  grid-template-rows: minmax(0, 1fr) minmax(220px, 36%);
}

.observability-panel__footer {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 12px 18px 16px;
  border-top: 1px solid rgba(255, 255, 255, 0.08);
  background: rgba(6, 8, 12, 0.96);
  overflow-x: auto;
}

.footer-chip {
  flex: none;
  min-height: 28px;
  padding: 0 10px;
  border: 1px solid rgba(255, 255, 255, 0.06);
  background: rgba(255, 255, 255, 0.03);
  display: inline-flex;
  align-items: center;
  font-size: 11px;
  letter-spacing: 0.12em;
  text-transform: uppercase;
  color: var(--color-text-secondary);
}

@media (max-width: 1365px) {
  .observability-panel__body {
    grid-template-rows: minmax(0, 1fr) minmax(260px, 40%);
  }
}
</style>
