<script setup lang="ts">
import { ref, onBeforeUnmount } from 'vue'

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
    holdTimer = setInterval(fn, 100)
  }, 300)
}

function stopHold() {
  if (holdTimeout) { clearTimeout(holdTimeout); holdTimeout = null }
  if (holdTimer) { clearInterval(holdTimer); holdTimer = null }
}

onBeforeUnmount(stopHold)

const atMax = ref(false)
const atMin = ref(false)
</script>

<template>
  <div class="number-stepper" :class="{ disabled }">
    <span v-if="label" class="stepper-label">{{ label }}</span>
    <button
      class="stepper-btn up"
      :disabled="disabled || modelValue >= max"
      @click="increment"
      @pointerdown="startHold(increment)"
      @pointerup="stopHold"
      @pointerleave="stopHold"
    >
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="18 15 12 9 6 15" /></svg>
    </button>
    <div class="stepper-value">
      <span class="value-text">{{ modelValue }}</span>
      <span v-if="unit" class="value-unit">{{ unit }}</span>
    </div>
    <button
      class="stepper-btn down"
      :disabled="disabled || modelValue <= min"
      @click="decrement"
      @pointerdown="startHold(decrement)"
      @pointerup="stopHold"
      @pointerleave="stopHold"
    >
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="6 9 12 15 18 9" /></svg>
    </button>
  </div>
</template>

<style scoped>
.number-stepper {
  display: flex;
  flex-direction: column;
  align-items: center;
  width: 80px;
  background: rgba(97, 97, 97, 0.6);
  border-radius: 8px;
  padding: 6px 4px;
  gap: 2px;
}

.number-stepper.disabled {
  opacity: 0.35;
  pointer-events: none;
}

.stepper-label {
  font-size: 10px;
  color: var(--color-text-secondary);
  margin-bottom: 2px;
}

.stepper-btn {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 32px;
  height: 24px;
  border: none;
  border-radius: 4px;
  background: transparent;
  color: var(--color-text-secondary);
  cursor: pointer;
  transition: all var(--transition-fast);
}

.stepper-btn:hover:not(:disabled) {
  color: var(--color-primary);
  background: rgba(255, 255, 255, 0.06);
}

.stepper-btn:active:not(:disabled) {
  transform: scale(0.9);
}

.stepper-btn:disabled {
  opacity: 0.25;
  cursor: not-allowed;
}

.stepper-value {
  display: flex;
  align-items: baseline;
  gap: 2px;
  padding: 4px 0;
}

.value-text {
  font-size: 22px;
  font-weight: 700;
  color: var(--color-primary);
  line-height: 1;
}

.value-unit {
  font-size: 12px;
  color: var(--color-text-secondary);
}
</style>
