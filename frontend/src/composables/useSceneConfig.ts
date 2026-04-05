import { ref, watch } from 'vue'
import { useWorldStore } from '@/stores/worldStore'
import type { SceneConfig } from '@/types/scene-config'

const cache = new Map<string, SceneConfig>()

export function useSceneConfig() {
  const worldStore = useWorldStore()
  const config = ref<SceneConfig | null>(null)
  const loading = ref(false)
  const error = ref<string | null>(null)

  async function loadConfig(sceneId: string) {
    if (!sceneId) return
    if (cache.has(sceneId)) {
      config.value = cache.get(sceneId)!
      return
    }
    loading.value = true
    error.value = null
    try {
      const resp = await fetch(`/scenes/${sceneId}.json`)
      if (!resp.ok) throw new Error(`Scene config not found: ${sceneId}`)
      const data: SceneConfig = await resp.json()
      cache.set(sceneId, data)
      config.value = data
    } catch (e: any) {
      error.value = e.message
    } finally {
      loading.value = false
    }
  }

  watch(() => worldStore.sceneId, (newId) => {
    if (newId) loadConfig(newId)
  }, { immediate: true })

  return { config, loading, error }
}
