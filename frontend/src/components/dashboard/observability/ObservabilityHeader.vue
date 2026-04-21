<script setup lang="ts">
import { computed } from 'vue'
import type {
  ConnectionStateDerived,
  EventCategoryFilter,
  EventFilters,
  EpisodeSummary,
} from '@/types/sim-event'
import { formatEventWallTime, getEventTypeLabel } from '@/utils/observability'

interface AgentOption {
  id: string
  label: string
}

const props = defineProps<{
  connectionState: ConnectionStateDerived
  currentEpisode: EpisodeSummary | null
  episodes: EpisodeSummary[]
  filters: EventFilters
  agentOptions: AgentOption[]
}>()

const emit = defineEmits<{
  close: []
  retry: []
  selectEpisode: [correlationId: string]
  updateFilters: [filters: Partial<EventFilters>]
}>()

const connectionLabel = computed(() => {
  switch (props.connectionState) {
    case 'connecting':
      return '连接中'
    case 'connected':
      return '在线'
    default:
      return '离线'
  }
})

const currentAgentLabel = computed(() => {
  if (!props.currentEpisode?.primaryAgentId) {
    return '未关联 Agent'
  }

  return props.agentOptions.find((agent) => agent.id === props.currentEpisode?.primaryAgentId)?.label
    ?? props.currentEpisode.primaryAgentId
})

function updateCategory(value: string) {
  emit('updateFilters', { category: value as EventCategoryFilter })
}

function updateAgent(value: string) {
  emit('updateFilters', { agentId: value })
}

function onCategoryChange(event: Event) {
  updateCategory((event.target as HTMLSelectElement).value)
}

function onAgentChange(event: Event) {
  updateAgent((event.target as HTMLSelectElement).value)
}

function updateFallbackOnly(event: Event) {
  const target = event.target as HTMLInputElement
  emit('updateFilters', { fallbackOnly: target.checked })
}
</script>

<template>
  <header class="observability-header">
    <div class="observability-header__top">
      <div>
        <p class="observability-header__eyebrow">可观测性</p>
        <h2 class="observability-header__title">Episode 链路</h2>
      </div>
      <div class="observability-header__actions">
        <span class="status-pill" :class="`status-pill--${connectionState}`">
          {{ connectionLabel }}
        </span>
        <button
          v-if="connectionState === 'disconnected'"
          class="action-btn"
          type="button"
          @click="emit('retry')"
        >
          重试
        </button>
        <button class="action-btn" type="button" aria-label="关闭观测侧栏" @click="emit('close')">
          关闭
        </button>
      </div>
    </div>

    <div class="current-episode">
      <div class="current-episode__meta">
        <span class="meta-chip meta-chip--accent">
          {{ currentEpisode ? getEventTypeLabel(currentEpisode.rootEventType) : '暂无 Episode' }}
        </span>
        <span class="meta-chip">{{ currentAgentLabel }}</span>
        <span v-if="currentEpisode?.hasFallback" class="meta-chip meta-chip--warning">规则回退</span>
      </div>
      <div class="current-episode__body">
        <p class="current-episode__title">
          {{ currentEpisode ? currentEpisode.correlationId : '等待新的事件链路' }}
        </p>
        <p class="current-episode__sub">
          {{ currentEpisode ? `${currentEpisode.eventCount} events · ${formatEventWallTime(currentEpisode.lastUpdatedAt)}` : '侧栏会自动跟随最新活跃 episode。' }}
        </p>
      </div>
    </div>

    <div class="filters-row">
      <label class="filter-field">
        <span class="filter-field__label">类别</span>
        <select class="filter-select" :value="filters.category" @change="onCategoryChange">
          <option value="all">全部</option>
          <option value="system">系统</option>
          <option value="environment">环境</option>
          <option value="user">用户</option>
          <option value="reasoning">推理</option>
          <option value="action">动作</option>
          <option value="feedback">反馈</option>
        </select>
      </label>

      <label class="filter-field">
        <span class="filter-field__label">执行 Agent</span>
        <select class="filter-select" :value="filters.agentId" @change="onAgentChange">
          <option value="all">全部</option>
          <option v-for="agent in agentOptions" :key="agent.id" :value="agent.id">
            {{ agent.label }}
          </option>
        </select>
      </label>

      <label class="fallback-toggle">
        <input type="checkbox" :checked="filters.fallbackOnly" @change="updateFallbackOnly" />
        <span>仅看回退链路</span>
      </label>
    </div>

    <div class="episode-list">
      <button
        v-for="episode in episodes"
        :key="episode.correlationId"
        class="episode-chip"
        :class="{ active: episode.correlationId === currentEpisode?.correlationId }"
        type="button"
        @click="emit('selectEpisode', episode.correlationId)"
      >
        <span class="episode-chip__title">{{ getEventTypeLabel(episode.rootEventType) }}</span>
        <span class="episode-chip__meta">
          {{ episode.primaryAgentId ?? 'No Agent' }} · {{ formatEventWallTime(episode.lastUpdatedAt) }}
        </span>
      </button>
    </div>
  </header>
</template>

<style scoped>
.observability-header {
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding: 18px 18px 16px;
  border-bottom: 1px solid rgba(255, 255, 255, 0.08);
  background: rgba(8, 10, 14, 0.96);
}

.observability-header__top,
.current-episode__meta,
.filters-row {
  display: flex;
  align-items: center;
  gap: 10px;
}

.observability-header__top {
  justify-content: space-between;
  align-items: flex-start;
}

.observability-header__eyebrow {
  margin: 0 0 6px;
  font-size: 10px;
  letter-spacing: 0.16em;
  text-transform: uppercase;
  color: var(--color-text-muted);
}

.observability-header__title {
  margin: 0;
  font-size: 22px;
  font-weight: 600;
  letter-spacing: -0.02em;
}

.observability-header__actions {
  display: flex;
  align-items: center;
  gap: 8px;
}

.status-pill,
.meta-chip,
.action-btn {
  min-height: 30px;
  padding: 0 10px;
  border: 1px solid rgba(255, 255, 255, 0.08);
  background: rgba(255, 255, 255, 0.03);
  color: var(--color-text-secondary);
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-size: 11px;
  letter-spacing: 0.1em;
  text-transform: uppercase;
}

.status-pill--connected {
  border-color: rgba(141, 223, 157, 0.2);
  color: var(--color-success);
}

.status-pill--connecting {
  border-color: rgba(102, 198, 255, 0.22);
  color: var(--color-cool);
}

.status-pill--disconnected,
.meta-chip--warning {
  border-color: rgba(248, 113, 113, 0.24);
  color: var(--color-danger);
}

.meta-chip--accent {
  border-color: rgba(255, 231, 74, 0.24);
  color: var(--color-primary);
}

.action-btn {
  cursor: pointer;
  transition-property: border-color, color, background-color, transform;
  transition-duration: var(--transition-fast);
  transition-timing-function: ease;
}

.action-btn:active {
  transform: scale(0.96);
}

@media (hover: hover) {
  .action-btn:hover {
    border-color: rgba(255, 231, 74, 0.34);
    color: var(--color-text-primary);
  }
}

.current-episode {
  padding: 12px;
  border: 1px solid rgba(255, 255, 255, 0.08);
  background: rgba(255, 255, 255, 0.03);
  box-shadow: inset 0 0 0 1px rgba(255, 255, 255, 0.02);
}

.current-episode__body {
  margin-top: 10px;
}

.current-episode__title,
.current-episode__sub {
  margin: 0;
}

.current-episode__title {
  font-size: 13px;
  color: var(--color-text-primary);
}

.current-episode__sub,
.filter-field__label,
.episode-chip__meta {
  font-size: 11px;
  color: var(--color-text-secondary);
}

.current-episode__sub {
  margin-top: 6px;
  line-height: 1.5;
}

.filters-row {
  flex-wrap: wrap;
}

.filter-field {
  display: flex;
  flex-direction: column;
  gap: 6px;
  min-width: 120px;
  flex: 1;
}

.filter-select {
  width: 100%;
  min-height: 34px;
  padding: 0 10px;
  border: 1px solid rgba(255, 255, 255, 0.08);
  background: rgba(255, 255, 255, 0.03);
  color: var(--color-text-primary);
}

.fallback-toggle {
  min-height: 34px;
  padding: 0 10px;
  border: 1px solid rgba(255, 255, 255, 0.08);
  background: rgba(255, 255, 255, 0.03);
  color: var(--color-text-secondary);
  display: inline-flex;
  align-items: center;
  gap: 8px;
}

.episode-list {
  max-height: 176px;
  overflow-y: auto;
  display: grid;
  gap: 8px;
}

.episode-chip {
  padding: 10px 12px;
  border: 1px solid rgba(255, 255, 255, 0.06);
  background: rgba(255, 255, 255, 0.02);
  text-align: left;
  color: var(--color-text-primary);
  cursor: pointer;
  transition-property: border-color, background-color, transform;
  transition-duration: var(--transition-fast);
  transition-timing-function: ease;
}

.episode-chip__title,
.episode-chip__meta {
  display: block;
}

.episode-chip__title {
  font-size: 12px;
  letter-spacing: 0.08em;
  text-transform: uppercase;
}

.episode-chip__meta {
  margin-top: 6px;
}

.episode-chip.active {
  border-color: rgba(255, 231, 74, 0.34);
  background: rgba(255, 231, 74, 0.08);
}

.episode-chip:active {
  transform: scale(0.98);
}

@media (hover: hover) {
  .episode-chip:hover {
    border-color: rgba(255, 255, 255, 0.16);
  }
}
</style>
