<script setup lang="ts">
import { computed, ref } from 'vue'
import type { RunSlot, RunSide } from '@/types/research-run'
import {
  alignRunEvents,
  compareReports,
  filterEventsByDeviceCorrelation,
  type AlignmentEvent,
} from '@/utils/runComparison'
import { getEventTypeLabel, summarizeSimEvent } from '@/utils/observability'

const props = defineProps<{
  slotA: RunSlot
  slotB: RunSlot
  activeDevice: string | null
}>()

const emit = defineEmits<{
  exportA: []
  exportB: []
  exportBundle: []
  clearDeviceFilter: []
}>()

const hideTimerTicks = ref(true)
const selectedAlignmentKey = ref<string | null>(null)
const mobileSide = ref<RunSide>('A')

const metricRows = computed(() => compareReports(props.slotA.report, props.slotB.report))
const timelineRows = computed(() => {
  const eventsA = filterEvents(props.slotA.events)
  const eventsB = filterEvents(props.slotB.events)
  return alignRunEvents(eventsA, eventsB)
})

const outcomeLabel = computed(() => (
  `${props.slotA.report?.outcome ?? '—'} / ${props.slotB.report?.outcome ?? '—'}`
))

function filterEvents(events: RunSlot['events']) {
  return filterEventsByDeviceCorrelation(events, props.activeDevice).filter((event) => {
    if (hideTimerTicks.value && event.event_type === 'system.timer_tick') return false
    return true
  })
}

function formatMetric(value: number | boolean | null | undefined, key: string, unit: string): string {
  if (value === null || value === undefined) return '—'
  if (typeof value === 'boolean') return value ? '是' : '否'
  if (key === 'device_state_match_rate') return `${(value * 100).toFixed(1)}%`
  if (key === 'first_action_latency_ms') return `${value.toFixed(1)} ${unit || 'ms'}`
  if (Number.isInteger(value)) return `${value}${unit === 'count' ? '' : unit}`
  return `${value.toFixed(3)}${unit}`
}

function formatDelta(delta: number | null, key: string, unit: string): string {
  if (delta === null) return '—'
  const sign = delta > 0 ? '+' : ''
  if (key === 'device_state_match_rate') return `${sign}${(delta * 100).toFixed(1)} pp`
  if (key === 'first_action_latency_ms') return `${sign}${delta.toFixed(1)} ${unit || 'ms'}`
  return `${sign}${Number.isInteger(delta) ? delta : delta.toFixed(3)}`
}

function judgmentLabel(winner: 'A' | 'B' | 'tie' | 'unavailable', informational: boolean): string {
  if (informational) return '仅观察'
  if (winner === 'tie') return '持平'
  if (winner === 'unavailable') return '缺数据'
  return `Run ${winner} 较优`
}

function selectRow(event: AlignmentEvent): void {
  selectedAlignmentKey.value = event.alignmentKey
}

function isSelected(event: AlignmentEvent | null): boolean {
  return Boolean(event && event.alignmentKey === selectedAlignmentKey.value)
}

function shortCorrelation(value: string): string {
  return value.length > 12 ? `${value.slice(0, 8)}…` : value
}
</script>

<template>
  <div class="comparison-view">
    <section class="comparison-summary" aria-labelledby="comparison-metrics-title">
      <header class="section-heading">
        <div>
          <p class="section-heading__eyebrow">CANONICAL EVALUATION · S4</p>
          <h2 id="comparison-metrics-title">七项指标</h2>
        </div>
        <div class="summary-actions">
          <span class="outcome-readout">OUTCOME {{ outcomeLabel }}</span>
          <button type="button" @click="emit('exportA')">A · JSONL</button>
          <button type="button" @click="emit('exportB')">B · JSONL</button>
          <button class="primary-action" type="button" @click="emit('exportBundle')">导出对比包</button>
        </div>
      </header>

      <div class="metric-table-wrap">
        <table class="metric-table">
          <thead>
            <tr>
              <th>Canonical metric</th>
              <th><span class="run-glyph run-glyph--a">A—</span> Run A</th>
              <th><span class="run-glyph run-glyph--b">B··</span> Run B</th>
              <th>Δ (B − A)</th>
              <th>判定</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in metricRows" :key="row.definition.key">
              <th scope="row">
                <span>{{ row.definition.label }}</span>
                <code>{{ row.definition.key }}</code>
              </th>
              <td :class="{ 'is-better': row.winner === 'A' }">
                {{ formatMetric(row.metricA?.value, row.definition.key, row.unit) }}
              </td>
              <td :class="{ 'is-better': row.winner === 'B' }">
                {{ formatMetric(row.metricB?.value, row.definition.key, row.unit) }}
              </td>
              <td>{{ formatDelta(row.delta, row.definition.key, row.unit) }}</td>
              <td>
                <span class="judgment" :class="`judgment--${row.winner}`">
                  {{ judgmentLabel(row.winner, row.definition.direction === 'informational') }}
                </span>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>

    <section class="timeline-comparison" aria-labelledby="comparison-timeline-title">
      <header class="section-heading section-heading--timeline">
        <div>
          <p class="section-heading__eyebrow">CAUSAL ALIGNMENT · RAW EVENTS</p>
          <h2 id="comparison-timeline-title">并排因果时间线</h2>
          <p class="section-heading__copy">
            按 root episode 语义序号与 correlation 内事件 occurrence 对齐；虚线空位表示该侧没有对应事件。
          </p>
        </div>
        <div class="timeline-controls">
          <label class="tick-toggle">
            <input v-model="hideTimerTicks" type="checkbox" />
            <span>隐藏 timer ticks</span>
          </label>
          <div v-if="activeDevice" class="device-filter">
            <span>3D DEVICE · {{ activeDevice }}</span>
            <button type="button" @click="emit('clearDeviceFilter')">清除</button>
          </div>
        </div>
      </header>

      <div class="mobile-side-tabs" aria-label="移动端运行侧选择">
        <button type="button" :class="{ active: mobileSide === 'A' }" @click="mobileSide = 'A'">A— Run A</button>
        <button type="button" :class="{ active: mobileSide === 'B' }" @click="mobileSide = 'B'">B·· Run B</button>
      </div>

      <div class="timeline-labels" aria-hidden="true">
        <span>A— RUN A · {{ slotA.run?.baseline_policy }}</span>
        <span>ALIGN</span>
        <span>B·· RUN B · {{ slotB.run?.baseline_policy }}</span>
      </div>

      <div v-if="timelineRows.length" class="aligned-timeline">
        <div v-for="row in timelineRows" :key="row.key" class="alignment-row">
          <button
            v-if="row.eventA"
            class="event-cell event-cell--a"
            :class="{ 'is-selected': isSelected(row.eventA), 'mobile-hidden': mobileSide !== 'A' }"
            :style="{ '--causal-depth': row.eventA.causalDepth }"
            type="button"
            @click="selectRow(row.eventA)"
          >
            <span class="event-cell__meta">
              <b>#{{ row.eventA.event.seq }}</b>
              <span>{{ shortCorrelation(row.eventA.event.correlation_id) }}</span>
            </span>
            <span class="event-cell__type">{{ getEventTypeLabel(row.eventA.event.event_type) }}</span>
            <span class="event-cell__summary">{{ summarizeSimEvent(row.eventA.event) }}</span>
          </button>
          <div v-else class="event-gap event-gap--a" :class="{ 'mobile-hidden': mobileSide !== 'A' }">
            <span>— gap</span>
          </div>

          <div class="alignment-rail" :class="`alignment-rail--${row.kind}`">
            <span>{{ row.kind === 'match' ? '=' : '·' }}</span>
          </div>

          <button
            v-if="row.eventB"
            class="event-cell event-cell--b"
            :class="{ 'is-selected': isSelected(row.eventB), 'mobile-hidden': mobileSide !== 'B' }"
            :style="{ '--causal-depth': row.eventB.causalDepth }"
            type="button"
            @click="selectRow(row.eventB)"
          >
            <span class="event-cell__meta">
              <b>#{{ row.eventB.event.seq }}</b>
              <span>{{ shortCorrelation(row.eventB.event.correlation_id) }}</span>
            </span>
            <span class="event-cell__type">{{ getEventTypeLabel(row.eventB.event.event_type) }}</span>
            <span class="event-cell__summary">{{ summarizeSimEvent(row.eventB.event) }}</span>
          </button>
          <div v-else class="event-gap event-gap--b" :class="{ 'mobile-hidden': mobileSide !== 'B' }">
            <span>— gap</span>
          </div>
        </div>
      </div>

      <div v-else class="timeline-empty">
        <strong>当前筛选下没有事件</strong>
        <span v-if="activeDevice">清除 3D 设备筛选，或返回场景点击其他设备。</span>
        <span v-else>原始工件为空，无法生成因果对齐。</span>
      </div>
    </section>
  </div>
</template>

<style scoped>
.comparison-view {
  display: grid;
  gap: 14px;
}

.comparison-summary,
.timeline-comparison {
  border: 1px solid rgba(255, 255, 255, 0.08);
  border-radius: 6px;
  background: #0a0d12;
  overflow: hidden;
}

.section-heading {
  padding: 14px 16px;
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  border-bottom: 1px solid rgba(255, 255, 255, 0.08);
  background: #10141a;
}

.section-heading__eyebrow,
.section-heading h2,
.section-heading__copy {
  margin: 0;
}

.section-heading__eyebrow {
  font-size: 9px;
  letter-spacing: 0.17em;
  color: var(--color-text-muted);
}

.section-heading h2 {
  margin-top: 5px;
  font-size: 17px;
  font-weight: 600;
}

.section-heading__copy {
  margin-top: 6px;
  max-width: 680px;
  font-size: 11px;
  line-height: 1.55;
  color: var(--color-text-secondary);
}

.summary-actions,
.timeline-controls,
.device-filter {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 7px;
}

.summary-actions button,
.device-filter button {
  min-height: 40px;
  padding: 0 11px;
  border: 1px solid rgba(255, 255, 255, 0.11);
  border-radius: 6px;
  background: rgba(255, 255, 255, 0.03);
  color: var(--color-text-secondary);
  cursor: pointer;
  transition-property: border-color, color, background-color, transform;
  transition-duration: var(--transition-fast);
}

.summary-actions .primary-action {
  border-color: rgba(255, 231, 74, 0.38);
  background: rgba(255, 231, 74, 0.1);
  color: var(--color-primary);
}

.summary-actions button:active,
.device-filter button:active {
  transform: scale(0.96);
}

.summary-actions button:focus-visible,
.device-filter button:focus-visible,
.event-cell:focus-visible,
.mobile-side-tabs button:focus-visible {
  outline: 2px solid var(--color-primary);
  outline-offset: 2px;
}

.outcome-readout {
  min-height: 40px;
  padding: 0 10px;
  display: inline-flex;
  align-items: center;
  border: 1px solid rgba(255, 255, 255, 0.08);
  border-radius: 2px;
  font-family: ui-monospace, "SFMono-Regular", Menlo, monospace;
  font-size: 10px;
  color: var(--color-text-muted);
}

.metric-table-wrap {
  overflow-x: auto;
}

.metric-table {
  width: 100%;
  min-width: 820px;
  border-collapse: collapse;
  font-variant-numeric: tabular-nums;
}

.metric-table th,
.metric-table td {
  padding: 11px 14px;
  border-bottom: 1px solid rgba(255, 255, 255, 0.06);
  text-align: left;
  font-size: 11px;
}

.metric-table thead th {
  background: #0c1015;
  font-size: 9px;
  letter-spacing: 0.12em;
  text-transform: uppercase;
  color: var(--color-text-muted);
}

.metric-table tbody th {
  font-weight: 500;
}

.metric-table tbody th span,
.metric-table tbody th code {
  display: block;
}

.metric-table tbody th code {
  margin-top: 3px;
  font-size: 9px;
  font-weight: 400;
  color: var(--color-text-muted);
}

.metric-table td {
  color: var(--color-text-secondary);
}

.metric-table td.is-better {
  color: var(--color-primary);
  font-weight: 650;
}

.run-glyph {
  margin-right: 3px;
  font-family: ui-monospace, "SFMono-Regular", Menlo, monospace;
  color: var(--color-primary);
}

.judgment {
  min-height: 26px;
  padding: 0 8px;
  display: inline-flex;
  align-items: center;
  border: 1px solid rgba(255, 255, 255, 0.07);
  border-radius: 999px;
  color: var(--color-text-muted);
}

.judgment--A,
.judgment--B {
  border-color: rgba(255, 231, 74, 0.22);
  color: var(--color-primary);
}

.section-heading--timeline {
  align-items: center;
}

.tick-toggle,
.device-filter {
  min-height: 40px;
  padding: 0 10px;
  border: 1px solid rgba(255, 255, 255, 0.08);
  border-radius: 6px;
  background: rgba(255, 255, 255, 0.025);
  font-size: 10px;
  color: var(--color-text-secondary);
}

.tick-toggle {
  display: inline-flex;
  align-items: center;
  gap: 8px;
}

.timeline-labels,
.alignment-row {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 54px minmax(0, 1fr);
}

.timeline-labels {
  padding: 9px 12px;
  border-bottom: 1px solid rgba(255, 255, 255, 0.07);
  background: #0c1015;
  font-family: ui-monospace, "SFMono-Regular", Menlo, monospace;
  font-size: 9px;
  letter-spacing: 0.1em;
  color: var(--color-text-muted);
}

.timeline-labels span:nth-child(2) {
  text-align: center;
}

.timeline-labels span:last-child {
  text-align: right;
}

.aligned-timeline {
  max-height: min(52vh, 620px);
  overflow-y: auto;
}

.alignment-row {
  min-height: 76px;
  border-bottom: 1px solid rgba(255, 255, 255, 0.05);
}

.event-cell,
.event-gap {
  min-width: 0;
  padding: 10px 12px 10px calc(12px + min(var(--causal-depth, 0), 5) * 10px);
  border: 0;
  background: #090c10;
  color: var(--color-text-primary);
  text-align: left;
}

.event-cell {
  display: grid;
  align-content: center;
  gap: 4px;
  cursor: pointer;
  transition-property: background-color, box-shadow, transform;
  transition-duration: var(--transition-fast);
}

.event-cell--a {
  box-shadow: inset 1px 0 0 rgba(255, 231, 74, 0.72);
}

.event-cell--b {
  background-image: repeating-linear-gradient(180deg, rgba(255, 231, 74, 0.08) 0 3px, transparent 3px 7px);
  background-size: 2px 100%;
  background-repeat: no-repeat;
}

.event-cell.is-selected {
  background-color: rgba(255, 231, 74, 0.09);
  box-shadow: inset 0 0 0 1px rgba(255, 231, 74, 0.32);
}

.event-cell:active {
  transform: scale(0.96);
}

.event-cell__meta {
  display: flex;
  gap: 8px;
  font-family: ui-monospace, "SFMono-Regular", Menlo, monospace;
  font-size: 9px;
  color: var(--color-text-muted);
}

.event-cell__meta b {
  color: var(--color-primary);
}

.event-cell__type {
  font-size: 11px;
  font-weight: 600;
}

.event-cell__summary {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 10px;
  color: var(--color-text-secondary);
}

.event-gap {
  display: grid;
  place-items: center;
  border: 1px dashed rgba(255, 255, 255, 0.08);
  color: var(--color-text-muted);
  font-size: 10px;
}

.alignment-rail {
  display: grid;
  place-items: center;
  border-right: 1px solid rgba(255, 255, 255, 0.05);
  border-left: 1px solid rgba(255, 255, 255, 0.05);
  background: #0d1116;
  color: var(--color-text-muted);
  font-family: ui-monospace, "SFMono-Regular", Menlo, monospace;
}

.alignment-rail--match {
  color: var(--color-primary);
}

.timeline-empty {
  min-height: 180px;
  padding: 24px;
  display: grid;
  place-content: center;
  gap: 7px;
  text-align: center;
  color: var(--color-text-secondary);
}

.timeline-empty span {
  font-size: 11px;
  color: var(--color-text-muted);
}

.mobile-side-tabs {
  display: none;
}

@media (hover: hover) {
  .summary-actions button:hover,
  .device-filter button:hover {
    border-color: rgba(255, 231, 74, 0.34);
    color: var(--color-text-primary);
  }

  .event-cell:hover {
    background-color: rgba(255, 255, 255, 0.035);
  }
}

@media (max-width: 720px) {
  .section-heading,
  .section-heading--timeline {
    align-items: stretch;
    flex-direction: column;
  }

  .summary-actions {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .outcome-readout,
  .summary-actions .primary-action {
    grid-column: 1 / -1;
  }

  .timeline-controls {
    align-items: stretch;
    flex-direction: column;
  }

  .mobile-side-tabs {
    padding: 8px;
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 6px;
    border-bottom: 1px solid rgba(255, 255, 255, 0.07);
  }

  .mobile-side-tabs button {
    min-height: 40px;
    border: 1px solid rgba(255, 255, 255, 0.08);
    border-radius: 6px;
    background: rgba(255, 255, 255, 0.03);
    color: var(--color-text-secondary);
  }

  .mobile-side-tabs button.active {
    border-color: rgba(255, 231, 74, 0.34);
    color: var(--color-primary);
  }

  .timeline-labels {
    display: none;
  }

  .alignment-row {
    display: grid;
    grid-template-columns: 1fr;
  }

  .alignment-rail,
  .mobile-hidden {
    display: none;
  }
}

@media (prefers-reduced-motion: reduce) {
  .summary-actions button,
  .device-filter button,
  .event-cell {
    transition-duration: 0.01ms;
  }
}
</style>
