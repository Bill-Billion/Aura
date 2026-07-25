<script setup lang="ts">
import { computed, ref } from 'vue'
import type { EvalReport, MetricDatum } from '@/types/eval-report'

const props = defineProps<{
  runIdA: string
  runIdB: string
  reportA: EvalReport | null
  reportB: EvalReport | null
  loadingA: boolean
  loadingB: boolean
  errorA: string | null
  errorB: string | null
}>()

const emit = defineEmits<{
  close: []
}>()

const selectedMetric = ref<string>('episode_completeness')

const metricOptions = [
  { id: 'episode_completeness', label: 'Episode 完整性' },
  { id: 'first_action_latency_ms', label: '首次动作延迟' },
  { id: 'command_success_rate', label: '命令成功率' },
  { id: 'fallback_rate', label: 'LLM 回退率' },
  { id: 'coordination_effectiveness', label: '仲裁有效性' },
  { id: 'safety_compliance', label: '安全合规' },
  { id: 'device_effect_accuracy', label: '设备效果准确率' },
]

interface MetricComparison {
  name: string
  valueA: number
  valueB: number
  unit: string
  delta: number
  better: 'A' | 'B' | 'tie'
  higherIsBetter: boolean
}

const comparisons = computed<MetricComparison[]>(() => {
  if (!props.reportA || !props.reportB) return []

  const metricsA = props.reportA.metrics
  const metricsB = props.reportB.metrics

  return metricOptions.map((opt) => {
    const ma = (metricsA as unknown as Record<string, MetricDatum>)[opt.id] || { value: 0, unit: '' }
    const mb = (metricsB as unknown as Record<string, MetricDatum>)[opt.id] || { value: 0, unit: '' }
    const higherIsBetter = opt.id !== 'fallback_rate' && opt.id !== 'first_action_latency_ms'

    const delta = mb.value - ma.value
    let better: 'A' | 'B' | 'tie' = 'tie'
    if (Math.abs(delta) > 0.001) {
      const aBetter = higherIsBetter ? ma.value > mb.value : ma.value < mb.value
      const bBetter = higherIsBetter ? mb.value > ma.value : mb.value < ma.value
      better = aBetter ? 'A' : bBetter ? 'B' : 'tie'
    }

    return {
      name: opt.label,
      valueA: ma.value,
      valueB: mb.value,
      unit: ma.unit || mb.unit || '',
      delta,
      better,
      higherIsBetter,
    }
  })
})

const selectedComparison = computed(() =>
  comparisons.value.find((c) => {
    const opt = metricOptions.find((o) => o.label === c.name)
    return opt?.id === selectedMetric.value
  })
)

const outcomeSummary = computed(() => {
  if (!props.reportA || !props.reportB) return null
  return {
    outcomeA: props.reportA.outcome,
    outcomeB: props.reportB.outcome,
    scenarioA: props.reportA.scenario_id,
    scenarioB: props.reportB.scenario_id,
    seedA: props.reportA.seed,
    seedB: props.reportB.seed,
  }
})
</script>

<template>
  <div class="comparison-view">
    <div class="comparison-header">
      <h3>Run 对比视图</h3>
      <button class="btn-close" @click="emit('close')">✕</button>
    </div>

    <!-- Outcome Summary -->
    <div v-if="outcomeSummary" class="outcome-bar">
      <div class="run-label">Run A ({{ outcomeSummary.scenarioA || '?' }})</div>
      <div class="outcome-badge" :class="outcomeSummary.outcomeA">
        {{ outcomeSummary.outcomeA }}
      </div>
      <div class="vs">vs</div>
      <div class="outcome-badge" :class="outcomeSummary.outcomeB">
        {{ outcomeSummary.outcomeB }}
      </div>
      <div class="run-label">Run B ({{ outcomeSummary.scenarioB || '?' }})</div>
    </div>

    <!-- Metric Selector -->
    <div class="metric-tabs">
      <button
        v-for="opt in metricOptions"
        :key="opt.id"
        class="metric-tab"
        :class="{ active: selectedMetric === opt.id }"
        @click="selectedMetric = opt.id"
      >
        {{ opt.label }}
      </button>
    </div>

    <!-- Metric Detail -->
    <div v-if="selectedComparison" class="metric-detail">
      <div class="bar-chart">
        <div class="bar-container">
          <div class="bar-label">Run A</div>
          <div class="bar-track">
            <div
              class="bar-fill bar-a"
              :style="{ width: barWidth(selectedComparison.valueA, selectedComparison.valueB, 'A') }"
            />
          </div>
          <div class="bar-value">{{ formatValue(selectedComparison.valueA) }}{{ selectedComparison.unit }}</div>
        </div>
        <div class="bar-container">
          <div class="bar-label">Run B</div>
          <div class="bar-track">
            <div
              class="bar-fill bar-b"
              :style="{ width: barWidth(selectedComparison.valueA, selectedComparison.valueB, 'B') }"
            />
          </div>
          <div class="bar-value">{{ formatValue(selectedComparison.valueB) }}{{ selectedComparison.unit }}</div>
        </div>
      </div>
      <div class="delta-badge" :class="selectedComparison.better">
        Δ = {{ formatDelta(selectedComparison.delta) }}{{ selectedComparison.unit }}
        <span v-if="selectedComparison.better !== 'tie'">
          ({{ selectedComparison.better === 'A' ? 'Run A' : 'Run B' }} 更优)
        </span>
      </div>
    </div>

    <!-- Full Comparison Table -->
    <div class="comparison-table">
      <table>
        <thead>
          <tr>
            <th>指标</th>
            <th>Run A</th>
            <th>Run B</th>
            <th>Δ</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="cmp in comparisons"
            :key="cmp.name"
            :class="{ selected: selectedMetric === metricOptions.find(o => o.label === cmp.name)?.id }"
            @click="selectedMetric = metricOptions.find(o => o.label === cmp.name)?.id || selectedMetric"
          >
            <td>{{ cmp.name }}</td>
            <td :class="{ better: cmp.better === 'A' }">{{ formatValue(cmp.valueA) }}{{ cmp.unit }}</td>
            <td :class="{ better: cmp.better === 'B' }">{{ formatValue(cmp.valueB) }}{{ cmp.unit }}</td>
            <td :class="cmp.better !== 'tie' ? `delta-${cmp.better.toLowerCase()}` : ''">
              {{ formatDelta(cmp.delta) }}
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- Detail Sections for Selected Metric -->
    <div v-if="selectedComparison && props.reportA && props.reportB" class="detail-sections">
      <div class="detail-panel">
        <h4>Run A 详情</h4>
        <pre class="detail-json">{{ JSON.stringify(getMetricDetails(props.reportA, selectedMetric), null, 2) }}</pre>
      </div>
      <div class="detail-panel">
        <h4>Run B 详情</h4>
        <pre class="detail-json">{{ JSON.stringify(getMetricDetails(props.reportB, selectedMetric), null, 2) }}</pre>
      </div>
    </div>
  </div>
</template>

<script lang="ts">
function formatValue(v: number): string {
  if (Math.abs(v) < 0.01) return v.toFixed(4)
  if (Math.abs(v) < 1) return v.toFixed(3)
  return v.toFixed(1)
}

function formatDelta(d: number): string {
  const sign = d >= 0 ? '+' : ''
  return `${sign}${formatValue(d)}`
}

function barWidth(valA: number, valB: number, side: 'A' | 'B'): string {
  const maxVal = Math.max(Math.abs(valA), Math.abs(valB), 0.01)
  const val = side === 'A' ? valA : valB
  return `${Math.min((Math.abs(val) / maxVal) * 100, 100)}%`
}

function getMetricDetails(report: EvalReport, metricId: string): Record<string, unknown> {
  const metrics = report.metrics as unknown as Record<string, { details?: Record<string, unknown> }>
  return metrics[metricId]?.details || {}
}
</script>

<style scoped>
.comparison-view {
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: 12px;
  padding: 16px;
  max-height: 80vh;
  overflow-y: auto;
}

.comparison-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 12px;
}

.comparison-header h3 {
  margin: 0;
  font-size: 16px;
  color: var(--color-text);
}

.btn-close {
  background: none;
  border: none;
  color: var(--color-text-muted);
  font-size: 18px;
  cursor: pointer;
}

.outcome-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 12px;
  background: var(--color-surface-raised);
  border-radius: 8px;
  margin-bottom: 12px;
}

.outcome-badge {
  padding: 2px 8px;
  border-radius: 4px;
  font-weight: 600;
  font-size: 12px;
  text-transform: uppercase;
}

.outcome-badge.pass { background: #22c55e20; color: #22c55e; }
.outcome-badge.fail { background: #ef444420; color: #ef4444; }
.outcome-badge.error { background: #f59e0b20; color: #f59e0b; }

.vs {
  color: var(--color-text-muted);
  font-weight: 600;
}

.metric-tabs {
  display: flex;
  gap: 4px;
  flex-wrap: wrap;
  margin-bottom: 12px;
}

.metric-tab {
  padding: 4px 10px;
  border: 1px solid var(--color-border);
  border-radius: 6px;
  background: transparent;
  color: var(--color-text-muted);
  font-size: 12px;
  cursor: pointer;
}

.metric-tab.active {
  background: var(--color-primary);
  color: white;
  border-color: var(--color-primary);
}

.bar-chart {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin-bottom: 12px;
}

.bar-container {
  display: flex;
  align-items: center;
  gap: 8px;
}

.bar-label {
  width: 48px;
  font-size: 12px;
  color: var(--color-text-muted);
}

.bar-track {
  flex: 1;
  height: 12px;
  background: var(--color-surface-raised);
  border-radius: 6px;
  overflow: hidden;
}

.bar-fill {
  height: 100%;
  border-radius: 6px;
  transition: width 0.3s ease;
}

.bar-a { background: var(--color-primary); }
.bar-b { background: #a78bfa; }

.bar-value {
  width: 80px;
  font-size: 12px;
  text-align: right;
  font-variant-numeric: tabular-nums;
}

.delta-badge {
  padding: 6px 12px;
  border-radius: 6px;
  font-size: 13px;
  text-align: center;
  margin-bottom: 12px;
}

.delta-badge.A { background: #22c55e10; color: #22c55e; }
.delta-badge.B { background: #a78bfa20; color: #a78bfa; }
.delta-badge.tie { background: var(--color-surface-raised); color: var(--color-text-muted); }

.comparison-table {
  margin-bottom: 12px;
  overflow-x: auto;
}

table {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
}

th, td {
  padding: 6px 10px;
  text-align: left;
  border-bottom: 1px solid var(--color-border);
}

th {
  color: var(--color-text-muted);
  font-weight: 500;
}

tr:hover td { background: var(--color-surface-raised); }
tr.selected td { background: var(--color-primary)10; }

td.better { color: #22c55e; font-weight: 600; }
.delta-a { color: #22c55e; }
.delta-b { color: #a78bfa; }

.detail-sections {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
}

.detail-panel h4 {
  margin: 0 0 8px;
  font-size: 13px;
  color: var(--color-text-muted);
}

.detail-json {
  background: var(--color-surface-raised);
  padding: 8px;
  border-radius: 6px;
  font-size: 11px;
  line-height: 1.4;
  max-height: 200px;
  overflow-y: auto;
  white-space: pre-wrap;
  word-break: break-all;
}
</style>
