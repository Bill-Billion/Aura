<script setup lang="ts">
import { computed } from 'vue'
import type { EpisodeNode, ObservabilityStateView } from '@/types/sim-event'
import { getEventTypeLabel, summarizeSimEvent } from '@/utils/observability'

const props = defineProps<{
  nodes: EpisodeNode[]
  selectedEventId: string | null
  panelState: ObservabilityStateView
}>()

const emit = defineEmits<{
  selectEvent: [eventId: string]
}>()

function onSelect(eventId: string) {
  emit('selectEvent', eventId)
}

const shouldShowPlaceholder = computed(() => (
  props.panelState.status === 'loading'
  || props.panelState.status === 'disconnected'
  || props.panelState.status === 'needs_start'
  || (props.panelState.status === 'empty' && props.nodes.length === 0)
))
</script>

<template>
  <section class="timeline-shell">
    <div class="section-head">
      <p class="section-head__eyebrow">Episode 时间线</p>
      <p class="section-head__sub">根事件 -> 推理 -> 动作 -> 反馈</p>
    </div>

    <div v-if="shouldShowPlaceholder" class="timeline-empty">
      <p class="timeline-empty__title">{{ panelState.title }}</p>
      <p class="timeline-empty__sub">{{ panelState.message }}</p>
    </div>

    <div v-else class="timeline-list">
      <button
        v-for="node in nodes"
        :key="node.eventId"
        class="timeline-node"
        :class="[
          `timeline-node--${node.category}`,
          {
            'is-selected': node.eventId === selectedEventId,
            'is-root': node.isRoot,
            'is-orphan': node.isOrphan,
          },
        ]"
        :style="{ '--depth': node.depth }"
        type="button"
        @click="onSelect(node.eventId)"
      >
        <span class="timeline-node__rail" />
        <span class="timeline-node__dot" />
        <span class="timeline-node__body">
          <span class="timeline-node__title">{{ getEventTypeLabel(node.event.event_type) }}</span>
          <span class="timeline-node__summary">{{ summarizeSimEvent(node.event) }}</span>
        </span>
      </button>
    </div>
  </section>
</template>

<style scoped>
.timeline-shell {
  min-height: 0;
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding: 16px 18px 14px;
  border-bottom: 1px solid rgba(255, 255, 255, 0.08);
  background: rgba(6, 8, 12, 0.88);
}

.section-head__eyebrow,
.section-head__sub {
  margin: 0;
}

.section-head__eyebrow {
  font-size: 10px;
  letter-spacing: 0.16em;
  text-transform: uppercase;
  color: var(--color-text-muted);
}

.section-head__sub {
  margin-top: 6px;
  font-size: 12px;
  color: var(--color-text-secondary);
}

.timeline-empty {
  min-height: 180px;
  border: 1px dashed rgba(255, 255, 255, 0.12);
  display: grid;
  place-items: center;
  padding: 18px;
  text-align: center;
  background: rgba(255, 255, 255, 0.02);
}

.timeline-empty__title,
.timeline-empty__sub {
  margin: 0;
}

.timeline-empty__title {
  font-size: 14px;
  color: var(--color-text-primary);
}

.timeline-empty__sub {
  margin-top: 8px;
  font-size: 12px;
  line-height: 1.6;
  color: var(--color-text-secondary);
}

.timeline-list {
  min-height: 0;
  overflow-y: auto;
  display: grid;
  gap: 8px;
}

.timeline-node {
  position: relative;
  display: flex;
  align-items: stretch;
  gap: 12px;
  padding: 10px 12px 10px calc(14px + (var(--depth) * 18px));
  border: 1px solid rgba(255, 255, 255, 0.05);
  background: rgba(255, 255, 255, 0.025);
  text-align: left;
  color: var(--color-text-primary);
  cursor: pointer;
  transition-property: border-color, background-color, transform;
  transition-duration: var(--transition-fast);
  transition-timing-function: ease;
}

.timeline-node__rail,
.timeline-node__dot {
  position: absolute;
  left: calc(12px + (var(--depth) * 18px));
}

.timeline-node__rail {
  top: 0;
  bottom: 0;
  width: 1px;
  background: rgba(255, 255, 255, 0.08);
}

.timeline-node__dot {
  top: 50%;
  width: 8px;
  height: 8px;
  border-radius: 50%;
  transform: translate(-50%, -50%);
  background: var(--timeline-accent, rgba(255, 255, 255, 0.28));
  box-shadow: 0 0 0 4px rgba(255, 255, 255, 0.04);
}

.timeline-node__body {
  display: flex;
  flex-direction: column;
  gap: 4px;
  min-width: 0;
}

.timeline-node__title,
.timeline-node__summary {
  display: block;
}

.timeline-node__title {
  font-size: 11px;
  letter-spacing: 0.14em;
  text-transform: uppercase;
}

.timeline-node__summary {
  font-size: 12px;
  line-height: 1.5;
  color: var(--color-text-secondary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.timeline-node--system {
  --timeline-accent: rgba(255, 255, 255, 0.38);
}

.timeline-node--environment {
  --timeline-accent: rgba(102, 198, 255, 0.78);
}

.timeline-node--user {
  --timeline-accent: rgba(141, 223, 157, 0.8);
}

.timeline-node--reasoning {
  --timeline-accent: rgba(170, 123, 255, 0.84);
}

.timeline-node--action {
  --timeline-accent: rgba(255, 231, 74, 0.86);
}

.timeline-node--feedback {
  --timeline-accent: rgba(239, 138, 109, 0.88);
}

.timeline-node.is-selected {
  border-color: rgba(255, 231, 74, 0.34);
  background: rgba(255, 231, 74, 0.08);
}

.timeline-node.is-root {
  box-shadow: inset 0 0 0 1px rgba(255, 255, 255, 0.03);
}

.timeline-node.is-orphan {
  border-style: dashed;
}

.timeline-node:active {
  transform: scale(0.985);
}

@media (hover: hover) {
  .timeline-node:hover {
    border-color: rgba(255, 255, 255, 0.14);
  }
}
</style>
