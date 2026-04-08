<script setup lang="ts">
import { computed, ref, watch, nextTick } from 'vue'
import { useAgentStore } from '@/stores/agentStore'
import { useWorldStore } from '@/stores/worldStore'
import { useWebSocket } from '@/composables/useWebSocket'
import { useUIStore } from '@/stores/uiStore'

const agentStore = useAgentStore()
const worldStore = useWorldStore()
const { sendCommand } = useWebSocket()
const uiStore = useUIStore()

const logEntries = computed(() => agentStore.actionLog)
const scrollContainer = ref<HTMLElement | null>(null)

watch(
  () => logEntries.value.length,
  async () => {
    await nextTick()
    if (scrollContainer.value) {
      scrollContainer.value.scrollTop = scrollContainer.value.scrollHeight
    }
  },
)

const agentColors: Record<string, string> = {
  lighting_agent: '#ffe74a',
  hvac_agent: '#4FC3F7',
  user_sim: '#4ade80',
}

function getAgentColor(name: string): string {
  return agentColors[name] ?? '#8b5cf6'
}

function formatTime(ts: number): string {
  const d = new Date(ts)
  return d.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit' })
}

const quickActions = [
  { label: '全部开灯', action: () => batchControl('light', 'turn_on') },
  { label: '全部关灯', action: () => batchControl('light', 'turn_off') },
  { label: '全部关闭', action: () => batchControl('all', 'turn_off') },
]

function batchControl(type: string, action: string) {
  for (const [id, dev] of Object.entries(worldStore.devices)) {
    if (type === 'all' || dev.type === type) {
      sendCommand('CMD_DEVICE_CONTROL', { device_id: id, action })
    }
  }
}

function close() {
  uiStore.sidebarOpen = false
}
</script>

<template>
  <div class="chat-panel">
    <div class="chat-header">
      <span class="header-title">AI 助手</span>
      <button class="close-btn" @click="close">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" /></svg>
      </button>
    </div>

    <div ref="scrollContainer" class="chat-messages">
      <template v-if="logEntries.length > 0">
        <div
          v-for="(entry, idx) in logEntries"
          :key="idx"
          class="message-bubble"
          :style="{ borderLeftColor: getAgentColor(entry.agent_name) }"
        >
          <div class="msg-header">
            <span class="msg-agent" :style="{ color: getAgentColor(entry.agent_name) }">
              {{ entry.agent_name }}
            </span>
            <span class="msg-time">{{ formatTime(entry.timestamp) }}</span>
          </div>
          <div class="msg-action">{{ entry.action }}</div>
          <div v-if="entry.reason" class="msg-reason">{{ entry.reason }}</div>
        </div>
      </template>
      <div v-else class="empty-state">
        等待 Agent 决策...
      </div>
    </div>

    <div class="chat-footer">
      <div class="quick-actions">
        <button
          v-for="qa in quickActions"
          :key="qa.label"
          class="quick-btn"
          @click="qa.action"
        >
          {{ qa.label }}
        </button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.chat-panel {
  display: flex;
  flex-direction: column;
  height: 100%;
  background: var(--panel-bg-solid);
  border-left: 1px solid var(--color-border-strong);
}

.chat-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 16px 16px 12px;
  border-bottom: 1px solid var(--color-border);
  flex-shrink: 0;
}

.header-title {
  font-size: 14px;
  font-weight: 700;
  color: var(--color-text-primary);
}

.close-btn {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 28px;
  height: 28px;
  border: none;
  border-radius: 6px;
  background: transparent;
  color: var(--color-text-secondary);
  cursor: pointer;
  transition: all var(--transition-fast);
}

.close-btn:hover {
  background: rgba(255, 255, 255, 0.08);
  color: var(--color-text-primary);
}

.chat-messages {
  flex: 1;
  overflow-y: auto;
  padding: 12px 16px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.message-bubble {
  padding: 8px 10px;
  border-left: 3px solid;
  border-radius: 0 6px 6px 0;
  background: rgba(255, 255, 255, 0.03);
}

.msg-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 4px;
}

.msg-agent {
  font-size: 11px;
  font-weight: 600;
}

.msg-time {
  font-size: 10px;
  color: var(--color-text-muted);
}

.msg-action {
  font-size: 12px;
  color: var(--color-text-primary);
  line-height: 1.4;
}

.msg-reason {
  font-size: 11px;
  color: var(--color-text-secondary);
  font-style: italic;
  margin-top: 3px;
  line-height: 1.3;
}

.empty-state {
  text-align: center;
  color: var(--color-text-muted);
  font-size: 12px;
  padding: 40px 0;
}

.chat-footer {
  border-top: 1px solid var(--color-border);
  padding: 12px 16px;
  flex-shrink: 0;
}

.quick-actions {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
}

.quick-btn {
  flex: 1;
  min-width: 0;
  padding: 6px 8px;
  border: 1px solid var(--color-border);
  border-radius: 6px;
  background: transparent;
  color: var(--color-text-secondary);
  font-size: 11px;
  cursor: pointer;
  transition: all var(--transition-fast);
  white-space: nowrap;
}

.quick-btn:hover {
  border-color: var(--color-primary);
  color: var(--color-primary);
  background: rgba(255, 231, 74, 0.06);
}
</style>
