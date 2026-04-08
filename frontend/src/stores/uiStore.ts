import { defineStore } from 'pinia'
import { ref } from 'vue'

export const useUIStore = defineStore('ui', () => {
  const activeRoom = ref<string>('living_room')
  const activeFloor = ref<string>('overview') // 'overview' | 'F1' | 'F2' | 'F3'
  const floorsExpanded = ref(false)
  const activeDevice = ref<string | null>(null)
  const sidebarOpen = ref(false)
  const sceneSelectorOpen = ref(false)

  function setActiveRoom(roomId: string) {
    activeRoom.value = roomId
  }

  function setActiveFloor(floorId: string) {
    activeFloor.value = floorId
  }

  function toggleFloorExpansion() {
    floorsExpanded.value = !floorsExpanded.value
  }

  function setActiveDevice(deviceId: string | null) {
    activeDevice.value = deviceId
  }

  function toggleSidebar() {
    sidebarOpen.value = !sidebarOpen.value
  }

  function toggleSceneSelector() {
    sceneSelectorOpen.value = !sceneSelectorOpen.value
  }

  return {
    activeRoom,
    activeFloor,
    floorsExpanded,
    activeDevice,
    sidebarOpen,
    sceneSelectorOpen,
    setActiveRoom,
    setActiveFloor,
    toggleFloorExpansion,
    setActiveDevice,
    toggleSidebar,
    toggleSceneSelector,
  }
})
