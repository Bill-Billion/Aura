import { defineStore } from 'pinia'
import { ref } from 'vue'
import type { SimEvent } from '@/types/sim-event'

const MAX_EVENTS = 200

export const useEventStore = defineStore('events', () => {
  const events = ref<SimEvent[]>([])

  function appendEvent(event: SimEvent) {
    events.value.push(event)
    if (events.value.length > MAX_EVENTS) {
      events.value = events.value.slice(-MAX_EVENTS)
    }
  }

  function clear() {
    events.value = []
  }

  return {
    events,
    appendEvent,
    clear,
  }
})
