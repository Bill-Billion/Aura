<script setup lang="ts">
import { computed } from 'vue'
import { useWorldStore } from '@/stores/worldStore'
import { useSimulationStore } from '@/stores/simulationStore'

const worldStore = useWorldStore()
const simulationStore = useSimulationStore()

const timeOfDay = computed(() => worldStore.environment.time_of_day)
const outdoorTemp = computed(() => worldStore.environment.outdoor_temp)
const humidity = computed(() => worldStore.environment.outdoor_humidity)
const weather = computed(() => worldStore.environment.weather)
const isRunning = computed(() => simulationStore.isRunning)
const tickCount = computed(() => worldStore.simulationTick)

const WEATHER_ICONS: Record<string, string> = {
  clear: '☀️',
  cloudy: '☁️',
  rainy: '🌧️',
  snowy: '❄️',
  stormy: '⛈️',
}

const weatherIcon = computed(() => WEATHER_ICONS[weather.value] ?? '☀️')
</script>

<template>
  <div class="status-bar glass-panel">
    <div class="weather-section">
      <span class="weather-icon">{{ weatherIcon }}</span>
      <div class="weather-info">
        <span class="temp-value">{{ outdoorTemp }}<span class="temp-unit">°C</span></span>
        <span class="humidity">💧 {{ humidity }}%</span>
      </div>
    </div>
    <div class="divider" />
    <div class="time-section">
      <span class="time-value">{{ timeOfDay }}</span>
    </div>
    <div class="divider" />
    <div class="status-section">
      <span class="status-dot" :class="{ running: isRunning }" />
      <span class="status-text">{{ isRunning ? '运行中' : '已暂停' }}</span>
    </div>
    <div class="divider" />
    <div class="tick-section">
      <span class="tick-label">T:</span>
      <span class="tick-value">{{ tickCount }}</span>
    </div>
  </div>
</template>

<style scoped>
.status-bar {
  display: flex;
  align-items: center;
  gap: 14px;
  padding: 10px 20px;
}

.weather-section {
  display: flex;
  align-items: center;
  gap: 8px;
}

.weather-icon {
  font-size: 24px;
}

.weather-info {
  display: flex;
  flex-direction: column;
  gap: 0;
}

.temp-value {
  font-size: 20px;
  font-weight: 700;
  color: var(--color-primary);
  line-height: 1.1;
}

.temp-unit {
  font-size: 12px;
  font-weight: 400;
  color: var(--color-text-secondary);
}

.humidity {
  font-size: 10px;
  color: var(--color-text-secondary);
}

.divider {
  width: 1px;
  height: 24px;
  background: var(--color-border);
}

.time-section {
  display: flex;
  align-items: center;
}

.time-value {
  font-size: 16px;
  font-weight: 600;
  color: var(--color-text-primary);
  font-variant-numeric: tabular-nums;
}

.status-section {
  display: flex;
  align-items: center;
  gap: 6px;
}

.status-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--color-text-muted);
  transition: all var(--transition-normal);
}

.status-dot.running {
  background: var(--color-primary);
  box-shadow: 0 0 8px rgba(255, 231, 74, 0.5);
  animation: pulse-glow 2s ease-in-out infinite;
}

.status-text {
  font-size: 12px;
  color: var(--color-text-secondary);
}

.tick-section {
  display: flex;
  align-items: center;
  gap: 3px;
}

.tick-label {
  font-size: 10px;
  color: var(--color-text-muted);
}

.tick-value {
  font-size: 11px;
  color: var(--color-text-secondary);
  font-variant-numeric: tabular-nums;
}
</style>
