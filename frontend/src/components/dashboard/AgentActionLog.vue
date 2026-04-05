<script setup lang="ts">
import { computed, ref, watch, nextTick } from 'vue'
import { useAgentStore } from '@/stores/agentStore'

const agentStore = useAgentStore()

const logEntries = computed(() => agentStore.actionLog)
const logContainer = ref<HTMLElement | null>(null)

// Auto-scroll to bottom on new entries
watch(
  () => logEntries.value.length,
  async () => {
    await nextTick()
    if (logContainer.value) {
      logContainer.value.scrollTop = logContainer.value.scrollHeight
    }
  },
)

const agentColors: Record<string, string> = {
  lighting_agent: '#f59e0b',
  hvac_agent: '#3b82f6',
  user_sim: '#10b981',
}

function getAgentColor(agentName: string): string {
  return agentColors[agentName] ?? '#8b5cf6'
}

function formatTime(ts: number): string {
  const d = new Date(ts)
  return d.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit' })
}
</script>

<template>
  <div class="agent-log glass-panel">
    <h3 class="log-title">Agent 决策日志</h3>
    <div ref="logContainer" class="log-scroll">
      <template v-if="logEntries.length > 0">
        <div
          v-for="(entry, idx) in logEntries"
          :key="idx"
          class="log-entry"
          :style="{ borderLeftColor: getAgentColor(entry.agent_name) }"
        >
          <div class="log-header">
            <span class="log-agent" :style="{ color: getAgentColor(entry.agent_name) }">
              {{ entry.agent_name }}
            </span>
            <span class="log-time">{{ formatTime(entry.timestamp) }}</span>
          </div>
          <div class="log-action">{{ entry.action }}</div>
          <div v-if="entry.reason" class="log-reason">{{ entry.reason }}</div>
        </div>
      </template>
      <div v-else class="log-empty">
        等待 Agent 决策...
      </div>
    </div>
  </div>
</template>

<style scoped>
.agent-log {
  position: absolute;
  top: 16px;
  right: 16px;
  width: 320px;
  max-height: calc(100vh - 100px);
  padding: 16px;
  z-index: 20;
  display: flex;
  flex-direction: column;
}

.log-title {
  font-size: 13px;
  font-weight: 600;
  color: #8a8aaa;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  margin: 0 0 12px 0;
  flex-shrink: 0;
}

.log-scroll {
  flex: 1;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.log-scroll::-webkit-scrollbar {
  width: 4px;
}

.log-scroll::-webkit-scrollbar-track {
  background: transparent;
}

.log-scroll::-webkit-scrollbar-thumb {
  background: rgba(255, 255, 255, 0.1);
  border-radius: 2px;
}

.log-entry {
  padding: 8px 10px;
  border-left: 3px solid #8b5cf6;
  border-radius: 0 6px 6px 0;
  background: rgba(255, 255, 255, 0.03);
}

.log-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 4px;
}

.log-agent {
  font-size: 11px;
  font-weight: 600;
}

.log-time {
  font-size: 10px;
  color: #666;
}

.log-action {
  font-size: 12px;
  color: #c0c0d0;
  line-height: 1.4;
}

.log-reason {
  font-size: 11px;
  color: #8a8aaa;
  font-style: italic;
  margin-top: 3px;
  line-height: 1.3;
}

.log-empty {
  text-align: center;
  color: #555;
  font-size: 12px;
  padding: 24px 0;
}
</style>
