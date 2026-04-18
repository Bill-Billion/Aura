<script setup lang="ts">
import { onBeforeUnmount } from 'vue'

const props = withDefaults(defineProps<{
  modelValue: number
  min: number
  max: number
  step?: number
  unit?: string
  label?: string
  disabled?: boolean
}>(), {
  step: 1,
})

const emit = defineEmits<{
  'update:modelValue': [value: number]
}>()

let holdTimer: ReturnType<typeof setInterval> | null = null
let holdTimeout: ReturnType<typeof setTimeout> | null = null

function increment() {
  if (props.disabled) return
  const next = Math.min(props.modelValue + props.step, props.max)
  if (next !== props.modelValue) emit('update:modelValue', next)
}

function decrement() {
  if (props.disabled) return
  const next = Math.max(props.modelValue - props.step, props.min)
  if (next !== props.modelValue) emit('update:modelValue', next)
}

function startHold(fn: () => void) {
  stopHold()
  holdTimeout = setTimeout(() => {
    holdTimer = setInterval(fn, 110)
  }, 320)
}

function stopHold() {
  if (holdTimeout) {
    clearTimeout(holdTimeout)
    holdTimeout = null
  }
  if (holdTimer) {
    clearInterval(holdTimer)
    holdTimer = null
  }
}

onBeforeUnmount(stopHold)
</script>

<template>
  <div class="number-stepper" :class="{ disabled }">
    <span v-if="label" class="stepper-label">{{ label }}</span>
    <button
      class="stepper-btn"
      :disabled="disabled || modelValue >= max"
      @click="increment"
      @pointerdown="startHold(increment)"
      @pointerup="stopHold"
      @pointerleave="stopHold"
    >
      +
    </button>
    <div class="stepper-value">
      <span class="value-text">{{ modelValue }}</span>
      <span v-if="unit" class="value-unit">{{ unit }}</span>
    </div>
    <button
      class="stepper-btn"
      :disabled="disabled || modelValue <= min"
      @click="decrement"
      @pointerdown="startHold(decrement)"
      @pointerup="stopHold"
      @pointerleave="stopHold"
    >
      -
    </button>
  </div>
</template>

<style scoped>
.number-stepper {
  display: flex;
  flex-direction: column;
  align-items: center;
  width: 96px;
  padding: 8px 6px;
  border: 1px solid var(--color-border);
  border-radius: 18px;
  background: rgba(255, 255, 255, 0.03);
  gap: 4px;
}

.number-stepper.disabled {
  opacity: 0.35;
  pointer-events: none;
}

.stepper-label,
.value-unit {
  font-size: 11px;
  color: var(--color-text-secondary);
}

.stepper-btn {
  width: 34px;
  height: 26px;
  border: 1px solid transparent;
  border-radius: 999px;
  background: rgba(255, 255, 255, 0.03);
  color: var(--color-text-secondary);
  cursor: pointer;
  transition: all var(--transition-fast);
}

.stepper-btn:hover:not(:disabled) {
  color: var(--color-primary);
  border-color: rgba(255, 231, 74, 0.34);
}

.stepper-btn:active:not(:disabled) {
  transform: scale(0.94);
}

.stepper-btn:disabled {
  opacity: 0.3;
  cursor: not-allowed;
}

.stepper-value {
  display: flex;
  align-items: baseline;
  gap: 4px;
}

.value-text {
  font-size: 24px;
  line-height: 1;
  letter-spacing: -0.05em;
  color: var(--color-primary);
}
</style>
