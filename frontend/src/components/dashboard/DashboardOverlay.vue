<script setup lang="ts">
import StatusBar from './StatusBar.vue'
import ControlPanel from './ControlPanel.vue'
import AgentActionLog from './AgentActionLog.vue'
</script>

<template>
  <div class="dashboard-overlay">
    <div class="dashboard-inner">
      <ControlPanel />
      <AgentActionLog />
      <StatusBar />
    </div>
  </div>
</template>

<style scoped>
.dashboard-overlay {
  position: absolute;
  inset: 0;
  pointer-events: none;
  z-index: 10;
}

.dashboard-inner {
  width: 100%;
  height: 100%;
  position: relative;
  pointer-events: auto;
}
</style>
