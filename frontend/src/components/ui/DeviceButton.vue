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
  <button
    class="device-btn"
    :class="{ active, disabled }"
    :disabled="disabled"
    @click="$emit('click')"
  >
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
  padding: 6px 14px;
  min-height: 32px;
  border: none;
  border-radius: 6px;
  background: rgba(97, 97, 97, 0.6);
  color: var(--color-text-secondary);
  font-size: 12px;
  font-weight: 500;
  cursor: pointer;
  transition: all var(--transition-normal);
  white-space: nowrap;
  user-select: none;
}

.device-btn:hover:not(:disabled):not(.active) {
  background: rgba(97, 97, 97, 0.8);
  color: var(--color-text-primary);
}

.device-btn:active:not(:disabled) {
  transform: scale(0.95);
}

.device-btn.active {
  background: var(--color-primary);
  color: #000;
  font-weight: 600;
}

.device-btn.disabled {
  opacity: 0.35;
  cursor: not-allowed;
}

.btn-icon {
  font-size: 14px;
}
</style>
