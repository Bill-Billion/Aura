<script setup lang="ts">
import { computed } from 'vue'
import type { EventDetailView, ObservabilityStateView } from '@/types/sim-event'

const props = defineProps<{
  detail: EventDetailView | null
  panelState: ObservabilityStateView
}>()

const shouldShowPlaceholder = computed(() => (
  props.panelState.status === 'loading'
  || props.panelState.status === 'disconnected'
  || props.panelState.status === 'needs_start'
  || (props.panelState.status === 'empty' && !props.detail)
))
</script>

<template>
  <section class="detail-shell">
    <div class="section-head">
      <p class="section-head__eyebrow">推理详情</p>
      <p class="section-head__sub">
        {{ detail ? detail.title : '选择事件查看 payload、命令和反馈字段。' }}
      </p>
    </div>

    <div v-if="panelState.fallbackMessage" class="fallback-banner">
      {{ panelState.fallbackMessage }}
    </div>

    <div v-if="shouldShowPlaceholder" class="detail-empty">
      <p class="detail-empty__title">{{ panelState.title }}</p>
      <p class="detail-empty__sub">{{ panelState.message }}</p>
    </div>

    <div v-else-if="detail" class="detail-body">
      <div v-if="detail.reasoningSteps?.length" class="reasoning-steps">
        <div
          v-for="step in detail.reasoningSteps"
          :key="step.eventType"
          class="reasoning-step"
          :class="`reasoning-step--${step.state}`"
        >
          <span class="reasoning-step__label">{{ step.label }}</span>
        </div>
      </div>

      <div v-if="detail.summary" class="detail-summary">
        {{ detail.summary }}
      </div>

      <div class="detail-fields">
        <div
          v-for="field in detail.fields"
          :key="field.label"
          class="detail-field"
          :class="field.tone ? `detail-field--${field.tone}` : ''"
        >
          <span class="detail-field__label">{{ field.label }}</span>
          <span class="detail-field__value">{{ field.value }}</span>
        </div>
      </div>

      <div v-if="detail.listItems?.length" class="detail-block">
        <p class="detail-block__title">{{ detail.listTitle }}</p>
        <ul class="detail-list">
          <li v-for="item in detail.listItems" :key="item" class="detail-list__item">
            {{ item }}
          </li>
        </ul>
      </div>

      <div v-if="detail.commands?.length" class="detail-block">
        <p class="detail-block__title">命令列表</p>
        <div class="command-list">
          <article v-for="command in detail.commands" :key="`${command.deviceId}-${command.property}`" class="command-card">
            <p class="command-card__title">{{ command.deviceId }}</p>
            <p class="command-card__meta">{{ command.property }} = {{ command.value }}</p>
            <p class="command-card__reason">{{ command.reason }}</p>
          </article>
        </div>
      </div>
    </div>
  </section>
</template>

<style scoped>
.detail-shell {
  min-height: 0;
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding: 16px 18px 18px;
  background: rgba(7, 9, 13, 0.94);
}

.section-head__eyebrow,
.section-head__sub,
.detail-empty__title,
.detail-empty__sub,
.command-card__title,
.command-card__meta,
.command-card__reason {
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
  line-height: 1.5;
  color: var(--color-text-secondary);
}

.fallback-banner {
  padding: 10px 12px;
  border: 1px solid rgba(248, 113, 113, 0.2);
  background: rgba(248, 113, 113, 0.08);
  color: #fca5a5;
  font-size: 12px;
}

.detail-empty {
  min-height: 180px;
  display: grid;
  place-items: center;
  padding: 18px;
  border: 1px dashed rgba(255, 255, 255, 0.12);
  text-align: center;
  background: rgba(255, 255, 255, 0.02);
}

.detail-empty__title {
  font-size: 14px;
  color: var(--color-text-primary);
}

.detail-empty__sub {
  margin-top: 8px;
  font-size: 12px;
  line-height: 1.6;
  color: var(--color-text-secondary);
}

.detail-body {
  min-height: 0;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.reasoning-steps {
  display: grid;
  grid-template-columns: repeat(6, minmax(0, 1fr));
  gap: 6px;
}

.reasoning-step {
  min-height: 38px;
  padding: 8px;
  border: 1px solid rgba(255, 255, 255, 0.06);
  background: rgba(255, 255, 255, 0.02);
  display: flex;
  align-items: center;
  justify-content: center;
}

.reasoning-step__label {
  font-size: 10px;
  letter-spacing: 0.12em;
  text-transform: uppercase;
  color: var(--color-text-secondary);
}

.reasoning-step--done {
  border-color: rgba(255, 231, 74, 0.16);
  background: rgba(255, 231, 74, 0.05);
}

.reasoning-step--done .reasoning-step__label,
.reasoning-step--current .reasoning-step__label {
  color: var(--color-text-primary);
}

.reasoning-step--current {
  border-color: rgba(255, 231, 74, 0.34);
  background: rgba(255, 231, 74, 0.12);
}

.detail-summary {
  padding: 12px;
  border-left: 1px solid rgba(255, 231, 74, 0.28);
  background: rgba(255, 255, 255, 0.02);
  font-size: 13px;
  line-height: 1.6;
  color: var(--color-text-primary);
}

.detail-fields {
  display: grid;
  gap: 8px;
}

.detail-field {
  padding: 10px 12px;
  border: 1px solid rgba(255, 255, 255, 0.06);
  background: rgba(255, 255, 255, 0.02);
  display: grid;
  gap: 6px;
}

.detail-field__label {
  font-size: 10px;
  letter-spacing: 0.14em;
  text-transform: uppercase;
  color: var(--color-text-muted);
}

.detail-field__value {
  font-size: 12px;
  line-height: 1.5;
  color: var(--color-text-primary);
  overflow-wrap: anywhere;
}

.detail-field--accent .detail-field__value {
  color: var(--color-primary);
}

.detail-field--warning .detail-field__value {
  color: #fca5a5;
}

.detail-block {
  display: grid;
  gap: 8px;
}

.detail-block__title {
  margin: 0;
  font-size: 11px;
  letter-spacing: 0.12em;
  text-transform: uppercase;
  color: var(--color-text-muted);
}

.detail-list {
  margin: 0;
  padding-left: 18px;
  display: grid;
  gap: 6px;
  color: var(--color-text-secondary);
  font-size: 12px;
}

.command-list {
  display: grid;
  gap: 8px;
}

.command-card {
  padding: 10px 12px;
  border: 1px solid rgba(255, 255, 255, 0.06);
  background: rgba(255, 255, 255, 0.03);
}

.command-card__title {
  font-size: 12px;
  color: var(--color-text-primary);
}

.command-card__meta,
.command-card__reason {
  margin-top: 6px;
  font-size: 11px;
  line-height: 1.5;
  color: var(--color-text-secondary);
}

@media (max-width: 1365px) {
  .reasoning-steps {
    grid-template-columns: repeat(3, minmax(0, 1fr));
  }
}
</style>
