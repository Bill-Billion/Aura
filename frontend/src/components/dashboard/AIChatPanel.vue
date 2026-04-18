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
  hvac_agent: '#66c6ff',
  user_sim: '#8ddf9d',
}

function getAgentColor(name: string): string {
  return agentColors[name] ?? '#b5bec8'
}

function formatTime(ts: number): string {
  return new Date(ts).toLocaleTimeString('zh-CN', {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  })
}

const quickActions = [
  { label: '全屋开灯', action: () => batchControl('light', 'turn_on') },
  { label: '全屋关灯', action: () => batchControl('light', 'turn_off') },
  { label: '设备归零', action: () => batchControl('all', 'turn_off') },
]

function batchControl(type: string, action: string) {
  for (const [id, dev] of Object.entries(worldStore.devices)) {
    if (type === 'all' || dev.type === type) {
      sendCommand('CMD_DEVICE_CONTROL', { device_id: id, action })
    }
  }
}
</script>

<template>
  <section class="chat-panel">
    <header class="chat-header">
      <div>
        <p class="chat-header__eyebrow">Auxiliary Feed</p>
        <h2 class="chat-header__title">Agent Log</h2>
      </div>
      <button class="close-btn" @click="uiStore.sidebarOpen = false">关闭</button>
    </header>

    <div ref="scrollContainer" class="chat-messages">
      <template v-if="logEntries.length > 0">
        <article
          v-for="(entry, idx) in logEntries"
          :key="idx"
          class="message-bubble"
          :style="{ '--agent-color': getAgentColor(entry.agent_name) }"
        >
          <div class="msg-header">
            <span class="msg-agent">{{ entry.agent_name }}</span>
            <span class="msg-time">{{ formatTime(entry.timestamp) }}</span>
          </div>
          <div class="msg-action">{{ entry.action }}</div>
          <div v-if="entry.reason" class="msg-reason">{{ entry.reason }}</div>
        </article>
      </template>
      <div v-else class="empty-state">当前没有新的 Agent 动作日志。</div>
    </div>

    <footer class="chat-footer">
      <button
        v-for="qa in quickActions"
        :key="qa.label"
        class="quick-btn"
        @click="qa.action"
      >
        {{ qa.label }}
      </button>
    </footer>
  </section>
</template>

<style scoped>
.chat-panel {
  display: flex;
  flex-direction: column;
  height: 100%;
  background: rgba(9, 12, 16, 0.96);
  border-left: 1px solid rgba(255, 255, 255, 0.06);
  backdrop-filter: blur(22px);
}

.chat-header {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 16px;
  padding: 20px 18px 16px;
  border-bottom: 1px solid rgba(255, 255, 255, 0.06);
}

.chat-header__eyebrow {
  margin: 0 0 6px;
  font-size: 10px;
  letter-spacing: 0.16em;
  text-transform: uppercase;
  color: var(--color-text-muted);
}

.chat-header__title {
  margin: 0;
  font-size: 22px;
  font-weight: 600;
}

.close-btn,
.quick-btn {
  border: 1px solid var(--color-border);
  border-radius: 999px;
  background: rgba(255, 255, 255, 0.03);
  color: var(--color-text-secondary);
  cursor: pointer;
  transition: all var(--transition-fast);
}

.close-btn {
  min-width: 58px;
  height: 30px;
}

.close-btn:hover,
.quick-btn:hover {
  color: var(--color-text-primary);
  border-color: rgba(255, 231, 74, 0.36);
}

.chat-messages {
  flex: 1;
  overflow-y: auto;
  padding: 14px 18px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.message-bubble {
  padding: 12px 14px;
  border: 1px solid rgba(255, 255, 255, 0.05);
  border-radius: 16px;
  background: rgba(255, 255, 255, 0.03);
  box-shadow: inset 0 0 0 1px rgba(255, 255, 255, 0.015);
}

.msg-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
}

.msg-agent {
  color: var(--agent-color);
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 0.14em;
}

.msg-time,
.msg-reason,
.empty-state {
  color: var(--color-text-secondary);
  font-size: 11px;
  line-height: 1.5;
}

.msg-action {
  font-size: 13px;
  line-height: 1.5;
}

.chat-footer {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 8px;
  padding: 16px 18px 20px;
  border-top: 1px solid rgba(255, 255, 255, 0.06);
}

.quick-btn {
  min-height: 36px;
}
</style>
