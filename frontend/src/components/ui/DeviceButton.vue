<script setup lang="ts">
defineProps<{
  active?: boolean
  label: string
  icon?: string
  disabled?: boolean
}>()

defineEmits<{
  click: []
}>()
</script>

<template>
  <button class="device-btn" :class="{ active, disabled }" :disabled="disabled" @click="$emit('click')">
    <span v-if="icon" class="btn-icon">{{ icon }}</span>
    <span class="btn-label">{{ label }}</span>
  </button>
</template>

<style scoped>
.device-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 4px;
  min-height: 34px;
  padding: 0 14px;
  border: 1px solid var(--color-border);
  border-radius: 999px;
  background: rgba(255, 255, 255, 0.03);
  color: var(--color-text-secondary);
  cursor: pointer;
  transition: all var(--transition-fast);
  white-space: nowrap;
}

.device-btn:hover:not(:disabled):not(.active) {
  border-color: rgba(255, 231, 74, 0.34);
  color: var(--color-text-primary);
}

.device-btn:active:not(:disabled) {
  transform: scale(0.95);
}

.device-btn.active {
  border-color: rgba(255, 231, 74, 0.48);
  background: rgba(255, 231, 74, 0.08);
  color: var(--color-primary);
}

.device-btn.disabled {
  opacity: 0.35;
  cursor: not-allowed;
}

.btn-label {
  font-size: 12px;
}
</style>
