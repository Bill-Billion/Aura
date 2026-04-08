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
        <span v-if="mode.icon" class="mode-icon">{{ mode.icon }}</span>
        <span class="mode-text">{{ mode.label }}</span>
      </button>
    </div>
  </div>
</template>

<style scoped>
.mode-selector {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.mode-selector.disabled {
  opacity: 0.35;
  pointer-events: none;
}

.mode-label {
  font-size: 10px;
  color: var(--color-text-secondary);
}

.mode-options {
  display: flex;
  gap: 6px;
}

.mode-btn {
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 5px 12px;
  border: 1px solid var(--color-border);
  border-radius: 16px;
  background: transparent;
  color: var(--color-text-secondary);
  font-size: 11px;
  cursor: pointer;
  transition: all var(--transition-normal);
  white-space: nowrap;
}

.mode-btn:hover:not(:disabled):not(.active) {
  border-color: var(--color-border-strong);
  color: var(--color-text-primary);
}

.mode-btn.active {
  background: var(--color-primary);
  border-color: var(--color-primary);
  color: #000;
  font-weight: 600;
}

.mode-icon {
  font-size: 13px;
}

.mode-text {
  line-height: 1;
}
</style>
