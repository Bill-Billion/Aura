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
        {{ opt.label }}
      </button>
    </div>
  </div>
</template>

<style scoped>
.level-selector {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.level-selector.disabled {
  opacity: 0.35;
  pointer-events: none;
}

.level-label {
  font-size: 11px;
  color: var(--color-text-secondary);
}

.level-options {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 6px;
}

.level-btn {
  min-height: 34px;
  border: 1px solid var(--color-border);
  border-radius: 999px;
  background: rgba(255, 255, 255, 0.03);
  color: var(--color-text-secondary);
  cursor: pointer;
  transition: all var(--transition-fast);
}

.level-btn:hover:not(:disabled):not(.active) {
  color: var(--color-text-primary);
  border-color: rgba(255, 231, 74, 0.34);
}

.level-btn.active {
  border-color: rgba(255, 231, 74, 0.48);
  background: rgba(255, 231, 74, 0.08);
  color: var(--color-primary);
}
</style>
