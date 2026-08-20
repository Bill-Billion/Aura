<script setup lang="ts">
import { computed } from 'vue'
import type { RunSlot } from '@/types/research-run'

const props = defineProps<{
  slot: RunSlot
}>()

const emit = defineEmits<{
  retry: []
  export: []
}>()

const phaseLabel = computed(() => ({
  empty: '空',
  loading: '请求中',
  pending: '运行中',
  success: '已完成',
  error: '错误',
})[props.slot.phase])

const policyLabel = computed(() => {
  if (!props.slot.config && !props.slot.run) return '待配置'
  return ({
    rule_based: '规则基线',
    llm_mocked: 'LLM Mock',
    llm_recorded: 'LLM 录制',
    llm_live: 'LLM Live',
  })[props.slot.config?.baseline_policy ?? 'rule_based']
})

const versionEntries = computed(() => {
  const run = props.slot.run
  if (!run) return []
  return [
    ['Scenario', run.scenario_schema_version],
    ['Event', run.event_schema_version],
    ['Command', run.command_schema_version],
    ['Registry', run.device_registry_version],
    ['Report', props.slot.report?.report_schema_version],
  ].filter((entry): entry is [string, string] => typeof entry[1] === 'string' && entry[1].length > 0)
})

function shortId(value: string | null | undefined): string {
  if (!value) return '—'
  return value.length > 24 ? `${value.slice(0, 12)}…${value.slice(-7)}` : value
}

function formatTime(value: string | null | undefined): string {
  if (!value) return '—'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleTimeString('zh-CN', { hour12: false })
}
</script>

<template>
  <article
    class="run-card"
    :class="[`run-card--${slot.side.toLowerCase()}`, `is-${slot.phase}`]"
    :aria-label="`Run ${slot.side} 状态`"
  >
    <header class="run-card__head">
      <div class="run-mark" aria-hidden="true">
        <span class="run-mark__letter">{{ slot.side }}</span>
        <span class="run-mark__rail" />
      </div>
      <div class="run-card__title-block">
        <p class="run-card__eyebrow">RUN {{ slot.side }} · {{ policyLabel }}</p>
        <h3 class="run-card__title">{{ shortId(slot.run?.run_id) }}</h3>
      </div>
      <span class="phase-badge" :class="`phase-badge--${slot.phase}`">
        {{ phaseLabel }}
      </span>
    </header>

    <div v-if="slot.phase === 'empty'" class="run-card__empty">
      <p>尚无实验工件</p>
      <span>在 Setup 中配置并启动这一侧。</span>
    </div>

    <div v-else class="run-card__body">
      <p class="run-card__stage" :role="slot.phase === 'error' ? 'alert' : 'status'">
        {{ slot.stage }}
      </p>

      <dl v-if="slot.config || slot.run" class="provenance-grid">
        <div>
          <dt>Scenario</dt>
          <dd>{{ slot.run?.scenario_id ?? slot.config?.scenario_id ?? '—' }}</dd>
        </div>
        <div>
          <dt>Seed</dt>
          <dd>{{ slot.run?.seed ?? slot.config?.seed ?? '—' }}</dd>
        </div>
        <div>
          <dt>Baseline policy</dt>
          <dd>{{ slot.run?.baseline_policy ?? slot.config?.baseline_policy ?? '—' }}</dd>
        </div>
        <div>
          <dt>Effective LLM mode</dt>
          <dd>{{ slot.run?.llm_mode ?? '等待 run 元数据' }}</dd>
        </div>
        <div>
          <dt>Provider / model</dt>
          <dd>{{ slot.run ? `${slot.run.llm_provider} / ${slot.run.llm_model}` : '—' }}</dd>
        </div>
        <div>
          <dt>Started / ended</dt>
          <dd>{{ formatTime(slot.run?.started_at) }} / {{ formatTime(slot.run?.ended_at) }}</dd>
        </div>
        <div>
          <dt>Events</dt>
          <dd>{{ slot.events.length }}</dd>
        </div>
        <div>
          <dt>Initial state hash</dt>
          <dd>{{ shortId(slot.run?.initial_state_hash) }}</dd>
        </div>
      </dl>

      <div v-if="versionEntries.length" class="version-row" aria-label="Schema versions">
        <span v-for="entry in versionEntries" :key="entry[0]">
          {{ entry[0] }} {{ entry[1] }}
        </span>
      </div>

      <div v-if="slot.error" class="run-error" role="alert">
        <strong>{{ slot.error.code }}</strong>
        <span>{{ slot.error.message }}</span>
        <code v-if="slot.error.details">{{ JSON.stringify(slot.error.details) }}</code>
      </div>

      <div class="run-card__actions">
        <button v-if="slot.phase === 'error' && slot.config" type="button" @click="emit('retry')">
          重试 Run {{ slot.side }}
        </button>
        <button
          v-if="slot.phase === 'success' && slot.run?.ended_at && slot.events.length"
          type="button"
          @click="emit('export')"
        >
          导出 raw JSONL
        </button>
      </div>
    </div>
  </article>
</template>

<style scoped>
.run-card {
  min-width: 0;
  border: 1px solid rgba(255, 255, 255, 0.09);
  border-radius: 6px;
  background: #0b0e13;
  box-shadow: 0 14px 32px rgba(0, 0, 0, 0.22);
  overflow: hidden;
}

.run-card__head {
  min-height: 68px;
  padding: 12px 14px;
  display: flex;
  align-items: center;
  gap: 12px;
  border-bottom: 1px solid rgba(255, 255, 255, 0.07);
  background: #10141a;
}

.run-mark {
  width: 42px;
  display: grid;
  gap: 5px;
  flex: none;
}

.run-mark__letter {
  font-family: ui-monospace, "SFMono-Regular", Menlo, monospace;
  font-size: 18px;
  font-weight: 700;
  color: var(--color-primary);
}

.run-mark__rail {
  display: block;
  height: 2px;
  background: var(--color-primary);
}

.run-card--b .run-mark__rail {
  background: repeating-linear-gradient(90deg, var(--color-primary) 0 5px, transparent 5px 9px);
}

.run-card__title-block {
  min-width: 0;
  flex: 1;
}

.run-card__eyebrow,
.run-card__title,
.run-card__stage,
.run-card__empty p,
.run-card__empty span {
  margin: 0;
}

.run-card__eyebrow {
  font-size: 10px;
  letter-spacing: 0.15em;
  color: var(--color-text-muted);
}

.run-card__title {
  margin-top: 5px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-family: ui-monospace, "SFMono-Regular", Menlo, monospace;
  font-size: 12px;
  font-weight: 500;
}

.phase-badge {
  min-height: 28px;
  padding: 0 9px;
  display: inline-flex;
  align-items: center;
  border: 1px solid rgba(255, 255, 255, 0.1);
  border-radius: 999px;
  font-size: 10px;
  letter-spacing: 0.1em;
  color: var(--color-text-secondary);
}

.phase-badge--loading,
.phase-badge--pending {
  border-color: rgba(255, 231, 74, 0.3);
  color: var(--color-primary);
}

.phase-badge--success {
  border-color: rgba(141, 223, 157, 0.28);
  color: var(--color-success);
}

.phase-badge--error {
  border-color: rgba(248, 113, 113, 0.3);
  color: var(--color-danger);
}

.run-card__empty {
  min-height: 140px;
  padding: 24px;
  display: grid;
  place-content: center;
  gap: 6px;
  text-align: center;
}

.run-card__empty p {
  font-size: 13px;
}

.run-card__empty span {
  font-size: 11px;
  color: var(--color-text-muted);
}

.run-card__body {
  padding: 13px;
  display: grid;
  gap: 12px;
}

.run-card__stage {
  font-size: 12px;
  color: var(--color-text-secondary);
}

.provenance-grid {
  margin: 0;
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 1px;
  background: rgba(255, 255, 255, 0.07);
}

.provenance-grid > div {
  min-width: 0;
  padding: 9px 10px;
  background: #0c1015;
}

.provenance-grid dt {
  margin-bottom: 4px;
  font-size: 9px;
  letter-spacing: 0.12em;
  text-transform: uppercase;
  color: var(--color-text-muted);
}

.provenance-grid dd {
  margin: 0;
  overflow-wrap: anywhere;
  font-family: ui-monospace, "SFMono-Regular", Menlo, monospace;
  font-size: 10px;
  line-height: 1.45;
  color: var(--color-text-secondary);
}

.version-row {
  display: flex;
  flex-wrap: wrap;
  gap: 5px;
}

.version-row span {
  padding: 4px 7px;
  border: 1px solid rgba(255, 255, 255, 0.07);
  border-radius: 2px;
  font-size: 9px;
  color: var(--color-text-muted);
}

.run-error {
  padding: 10px;
  display: grid;
  gap: 5px;
  border: 1px solid rgba(248, 113, 113, 0.22);
  border-radius: 2px;
  background: rgba(248, 113, 113, 0.07);
  font-size: 11px;
  line-height: 1.45;
  color: #fca5a5;
}

.run-error code {
  overflow-wrap: anywhere;
  color: var(--color-text-secondary);
}

.run-card__actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.run-card__actions button {
  min-height: 40px;
  padding: 0 12px;
  border: 1px solid rgba(255, 255, 255, 0.1);
  border-radius: 6px;
  background: rgba(255, 255, 255, 0.03);
  color: var(--color-text-secondary);
  cursor: pointer;
  transition-property: border-color, color, background-color, transform;
  transition-duration: var(--transition-fast);
}

.run-card__actions button:active {
  transform: scale(0.96);
}

.run-card__actions button:focus-visible {
  outline: 2px solid var(--color-primary);
  outline-offset: 2px;
}

@media (hover: hover) {
  .run-card__actions button:hover {
    border-color: rgba(255, 231, 74, 0.34);
    color: var(--color-text-primary);
  }
}
</style>
