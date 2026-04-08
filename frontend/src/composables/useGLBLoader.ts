import { ref, shallowReactive } from 'vue'
import * as THREE from 'three'
import { GLTFLoader, DRACOLoader, MeshoptDecoder } from 'three-stdlib'
import type { GLTF } from 'three-stdlib'
import type { FloorConfig } from '@/types/model-types'

const cache = new Map<string, GLTF>()

let loader: GLTFLoader | null = null

function getLoader(): GLTFLoader {
  if (loader) return loader
  loader = new GLTFLoader()

  // DRACO decoder
  const dracoLoader = new DRACOLoader()
  dracoLoader.setDecoderPath('https://www.gstatic.com/draco/versioned/decoders/1.5.7/')
  dracoLoader.setDecoderConfig({ type: 'js' })
  loader.setDRACOLoader(dracoLoader)

  // Meshopt decoder
  loader.setMeshoptDecoder(MeshoptDecoder)

  return loader
}

export function useGLBLoader() {
  const floors = shallowReactive<Map<string, GLTF>>(new Map())
  const progress = ref(0)
  const loading = ref(false)
  const error = ref<string | null>(null)

  async function loadFloor(config: FloorConfig): Promise<GLTF> {
    if (cache.has(config.id)) {
      const cached = cache.get(config.id)!
      floors.set(config.id, cached)
      return cached
    }

    const gltfLoader = getLoader()
    return new Promise((resolve, reject) => {
      gltfLoader.load(
        config.modelPath,
        (gltf) => {
          cache.set(config.id, gltf)
          floors.set(config.id, gltf)
          resolve(gltf)
        },
        (event) => {
          if (event.total > 0) {
            progress.value = event.loaded / event.total
          }
        },
        (err) => {
          reject(err)
        },
      )
    })
  }

  async function loadAll(configs: FloorConfig[]): Promise<void> {
    loading.value = true
    error.value = null
    try {
      // Load sequentially for progressive rendering (F1 first)
      for (const config of configs) {
        await loadFloor(config)
      }
    } catch (e: any) {
      error.value = e.message ?? 'Failed to load GLB models'
    } finally {
      loading.value = false
    }
  }

  return {
    floors,
    progress,
    loading,
    error,
    loadFloor,
    loadAll,
  }
}
