<script setup lang="ts">
defineProps<{
  modelValue: number | string
  options: Array<{ value: number | string; label: string }>
  label?: string
  disabled?: boolean
}>()

const emit = defineEmits<{
  'update:modelValue': [value: number | string]
}>()
</script>

<template>
  <div class="level-selector" :class="{ disabled }">
    <span v-if="label" class="level-label">{{ label }}</span>
    <div class="level-options">
      <button
        v-for="opt in options"
        :key="String(opt.value)"
        class="level-btn"
        :class="{ active: modelValue === opt.value }"
        :disabled="disabled"
        @click="emit('update:modelValue', opt.value)"
      >
        <span class="level-text">{{ opt.label }}</span>
        <div class="level-line" />
      </button>
    </div>
  </div>
</template>

<style scoped>
.level-selector {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.level-selector.disabled {
  opacity: 0.35;
  pointer-events: none;
}

.level-label {
  font-size: 10px;
  color: var(--color-text-secondary);
}

.level-options {
  display: flex;
  gap: 0;
}

.level-btn {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 4px;
  padding: 6px 8px;
  border: none;
  background: transparent;
  cursor: pointer;
  transition: all var(--transition-fast);
}

.level-text {
  font-size: 11px;
  color: var(--color-text-secondary);
  transition: color var(--transition-fast);
}

.level-btn.active .level-text {
  color: var(--color-primary);
  font-weight: 600;
}

.level-line {
  width: 100%;
  height: 2px;
  background: var(--color-text-muted);
  border-radius: 1px;
  transition: background var(--transition-fast);
}

.level-btn.active .level-line {
  background: var(--color-primary);
}

.level-btn:hover:not(:disabled):not(.active) .level-text {
  color: var(--color-text-primary);
}

.level-btn:hover:not(:disabled):not(.active) .level-line {
  background: var(--color-text-secondary);
}
</style>
