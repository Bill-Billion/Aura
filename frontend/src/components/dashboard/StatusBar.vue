<script setup lang="ts">
import { computed } from 'vue'
import { useWorldStore } from '@/stores/worldStore'
import { useSimulationStore } from '@/stores/simulationStore'

const worldStore = useWorldStore()
const simulationStore = useSimulationStore()

const timeOfDay = computed(() => worldStore.environment.time_of_day)
const outdoorTemp = computed(() => worldStore.environment.outdoor_temp)
const humidity = computed(() => {
  const raw = worldStore.environment.outdoor_humidity
  return raw <= 1 ? Math.round(raw * 100) : Math.round(raw)
})
const weather = computed(() => worldStore.environment.weather)
const isRunning = computed(() => simulationStore.isRunning)
const connectionStatus = computed(() => simulationStore.connectionStatus)

const weatherText = computed(() => {
  const labels: Record<string, string> = {
    clear: '晴朗',
    cloudy: '多云',
    rainy: '降雨',
    snowy: '降雪',
    stormy: '风暴',
  }
  return labels[weather.value] ?? '晴朗'
})

const connectionLabel = computed(() => {
  const labels: Record<string, string> = {
    connected: '已连接',
    connecting: '连接中',
    disconnected: '已断开',
    error: '连接异常',
  }
  return labels[connectionStatus.value] ?? connectionStatus.value
})
</script>

<template>
  <section class="status-bar showroom-card">
    <div class="status-bar__weather">
      <p class="status-bar__eyebrow">天气 / 时间</p>
      <div class="status-bar__headline-row">
        <span class="status-bar__temp">{{ outdoorTemp }}°C</span>
        <span class="status-bar__weather-text">{{ weatherText }}</span>
      </div>
      <p class="status-bar__sub">模拟住宅 · 湿度 {{ humidity }}% · {{ timeOfDay }}</p>
    </div>

    <div class="status-bar__meta">
      <div class="status-pill" :class="{ live: isRunning }">
        <span class="status-pill__dot" />
        <span>{{ isRunning ? '模拟运行中' : '模拟已暂停' }}</span>
      </div>
      <div class="status-pill" :class="connectionStatus">
        <span class="status-pill__dot" />
        <span>{{ connectionLabel }}</span>
      </div>
    </div>
  </section>
</template>

<style scoped>
.status-bar {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
}

.status-bar__weather {
  display: flex;
  flex-direction: column;
  gap: 5px;
}

.status-bar__eyebrow {
  margin: 0;
  color: var(--color-text-muted);
  font-size: 10px;
  letter-spacing: 0.18em;
  text-transform: uppercase;
}

.status-bar__headline-row {
  display: flex;
  align-items: baseline;
  gap: 10px;
}

.status-bar__temp {
  font-size: 42px;
  line-height: 0.92;
  letter-spacing: -0.05em;
  color: var(--color-primary);
}

.status-bar__weather-text {
  font-size: 16px;
  color: var(--color-text-primary);
}

.status-bar__sub {
  margin: 0;
  font-size: 12px;
  color: var(--color-text-secondary);
}

.status-bar__meta {
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: 8px;
}

.status-pill {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  min-height: 30px;
  padding: 0 12px;
  border-radius: 999px;
  border: 1px solid var(--color-border);
  background: rgba(255, 255, 255, 0.02);
  color: var(--color-text-secondary);
  font-size: 11px;
}

.status-pill__dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: rgba(255, 255, 255, 0.2);
}

.status-pill.live .status-pill__dot,
.status-pill.connected .status-pill__dot {
  background: var(--color-primary);
  box-shadow: 0 0 10px rgba(255, 231, 74, 0.28);
}

.status-pill.error .status-pill__dot,
.status-pill.disconnected .status-pill__dot {
  background: #f87171;
  box-shadow: 0 0 10px rgba(248, 113, 113, 0.26);
}

.status-pill.connecting .status-pill__dot {
  background: #fbbf24;
}

@media (max-width: 960px) {
  .status-bar {
    flex-direction: column;
    align-items: flex-start;
  }

  .status-bar__meta {
    width: 100%;
    align-items: flex-start;
    flex-direction: row;
    flex-wrap: wrap;
  }
}
</style>
