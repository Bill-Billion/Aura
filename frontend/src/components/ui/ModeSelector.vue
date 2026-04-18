<script setup lang="ts">
defineProps<{
  modelValue: string
  modes: Array<{ value: string; label: string; icon?: string }>
  label?: string
  disabled?: boolean
}>()

const emit = defineEmits<{
  'update:modelValue': [value: string]
}>()
</script>

<template>
  <div class="mode-selector" :class="{ disabled }">
    <span v-if="label" class="mode-label">{{ label }}</span>
    <div class="mode-options">
      <button
        v-for="mode in modes"
        :key="mode.value"
        class="mode-btn"
        :class="{ active: modelValue === mode.value }"
        :disabled="disabled"
        @click="emit('update:modelValue', mode.value)"
      >
        <span class="mode-text">{{ mode.label }}</span>
      </button>
    </div>
  </div>
</template>

<style scoped>
.mode-selector {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.mode-selector.disabled {
  opacity: 0.35;
  pointer-events: none;
}

.mode-label {
  font-size: 11px;
  color: var(--color-text-secondary);
}

.mode-options {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 6px;
}

.mode-btn {
  min-height: 34px;
  border: 1px solid var(--color-border);
  border-radius: 999px;
  background: rgba(255, 255, 255, 0.03);
  color: var(--color-text-secondary);
  font-size: 12px;
  cursor: pointer;
  transition: all var(--transition-fast);
}

.mode-btn:hover:not(:disabled):not(.active) {
  color: var(--color-text-primary);
  border-color: rgba(255, 231, 74, 0.34);
}

.mode-btn.active {
  border-color: rgba(255, 231, 74, 0.48);
  background: rgba(255, 231, 74, 0.08);
  color: var(--color-primary);
}
</style>
