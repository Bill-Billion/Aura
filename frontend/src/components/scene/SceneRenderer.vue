<script setup lang="ts">
import { onBeforeUnmount, onMounted, shallowRef, watch, ref } from 'vue'
import { TresCanvas } from '@tresjs/core'
import * as THREE from 'three'
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js'
import { HDRLoader } from 'three/addons/loaders/HDRLoader.js'
import { CSS2DObject, CSS2DRenderer } from 'three/addons/renderers/CSS2DRenderer.js'
import gsap from 'gsap'
import { useUIStore } from '@/stores/uiStore'
import { useWorldStore } from '@/stores/worldStore'
import { useSphericalCamera } from '@/composables/useSphericalCamera'
import { useShaderMaterials, type LightGroupConfig } from '@/composables/useShaderMaterials'
import { useLightUniforms } from '@/composables/useLightUniforms'
import {
  initDeviceAnimStore,
  registerDeviceNodes,
  setupLightWatchers,
} from '@/composables/useDeviceAnimations'
import { showroomVisualConfig, type ShowroomMaterialRole } from '@/config/showroomVisualConfig'
import { getFloorForDevice } from '@/utils/deviceFloorMap'
import { showroomRuntime } from './showroomRuntime'
import { SceneRenderLoop } from './SceneRenderLoop'
import groundVert from '@/shaders/ground/vertex.glsl?raw'
import groundFrag from '@/shaders/ground/fragment.glsl?raw'

type FloorId = 'F1' | 'F2' | 'F3'

const uiStore = useUIStore()
const worldStore = useWorldStore()
const camera = useSphericalCamera()
const lightUniforms = useLightUniforms()
initDeviceAnimStore(worldStore)

const sceneHostEl = ref<HTMLElement | null>(null)
const glbLoader = new GLTFLoader()
const textureLoader = new THREE.TextureLoader()
const raycaster = new THREE.Raycaster()
const pointer = new THREE.Vector2()

const floorRefs: Record<FloorId, ReturnType<typeof shallowRef<THREE.Group | null>>> = {
  F1: shallowRef<THREE.Group | null>(null),
  F2: shallowRef<THREE.Group | null>(null),
  F3: shallowRef<THREE.Group | null>(null),
}

const reflectionRefs: Record<FloorId, ReturnType<typeof shallowRef<THREE.Group | null>>> = {
  F1: shallowRef<THREE.Group | null>(null),
  F2: shallowRef<THREE.Group | null>(null),
  F3: shallowRef<THREE.Group | null>(null),
}

const labelElements = new Map<FloorId, HTMLDivElement>()
const selectableMeshes = new Map<THREE.Object3D, string>()
const floorForSelectable = new Map<THREE.Object3D, FloorId>()

const groundMaterial = new THREE.ShaderMaterial({
  vertexShader: groundVert,
  fragmentShader: groundFrag,
  uniforms: {
    u_centerColor: { value: new THREE.Color(0x12161d) },
    u_edgeColor: { value: new THREE.Color(0x06080c) },
    u_shadowColor: { value: new THREE.Color(0x07090d) },
    u_reflectionColor: { value: new THREE.Color(0x8897a6) },
    u_center: { value: new THREE.Vector2(0.48, 0.47) },
    u_radius: { value: 0.6 },
    u_time: { value: 0 },
  },
  transparent: true,
  depthWrite: false,
  toneMapped: false,
})

const floorOrder: FloorId[] = ['F1', 'F2', 'F3']

const cameraPresets = {
  overview: showroomVisualConfig.camera.overview,
  F1: showroomVisualConfig.camera.floors.F1,
  F2: showroomVisualConfig.camera.floors.F2,
  F3: showroomVisualConfig.camera.floors.F3,
}

let floorsExpanded = false
let canvasEl: HTMLCanvasElement | null = null
let pointerDown = { x: 0, y: 0 }
let labelRenderer: CSS2DRenderer | null = null

const FLOOR_DISPLAY_NAMES: Record<FloorId, string> = {
  F1: 'F1 Living Deck',
  F2: 'F2 Private Deck',
  F3: 'F3 Service Deck',
}

function loadGLB(url: string): Promise<THREE.Group> {
  return new Promise((resolve, reject) => {
    glbLoader.load(url, (gltf) => resolve(gltf.scene), undefined, reject)
  })
}

function loadTexture(url: string): Promise<THREE.Texture> {
  return new Promise((resolve, reject) => {
    textureLoader.load(url, resolve, undefined, reject)
  })
}

function extractAOMap(scene: THREE.Group): THREE.Texture | null {
  let aoMap: THREE.Texture | null = null
  scene.traverse((obj) => {
    if (aoMap || !(obj instanceof THREE.Mesh)) return
    const material = Array.isArray(obj.material) ? obj.material[0] : obj.material
    const standardMaterial = material as THREE.MeshStandardMaterial
    if (standardMaterial?.aoMap) {
      aoMap = standardMaterial.aoMap
    }
  })
  return aoMap
}

function classifyRole(nodeName: string, materialName: string): ShowroomMaterialRole {
  const lowerName = nodeName.toLowerCase()
  const lowerMat = materialName.toLowerCase()

  if (lowerName === 'wall' || lowerMat.includes('glass') || lowerMat.includes('wall')) return 'wallGlass'
  if (lowerName === 'floor' || lowerMat.includes('woodfloor') || lowerMat === 'floor') return 'floorDeck'
  if (lowerName === 'car' || lowerName.includes('sweeper')) return 'vehicleFx'
  if (lowerName.startsWith('cam')) return 'signage'
  if (/^(ac|air|fan|fridge|wash|charge|hotwater|tv|radiator)/.test(lowerName) || lowerMat.includes('black')) return 'applianceMetal'
  return 'furniture'
}

function resolveSceneDeviceId(floorId: FloorId, nodeName: string): string | null {
  const lowerName = nodeName.toLowerCase()
  if (floorId === 'F1' && lowerName.startsWith('ac')) return 'ac_living_01'
  if (floorId === 'F1' && lowerName.startsWith('curtain')) return 'curtain_living_01'
  return null
}

function createLightGroups(floorId: FloorId, uniforms: THREE.Vector4[]) {
  const floor = showroomVisualConfig.floors[floorId]
  const bias = floor.lightBias
  const makeLights = (size: [number, number, number]) => {
    return floor.lights.map((position, index) => ({
      position: new THREE.Vector4(position[0], position[1], position[2], uniforms[index]?.w ?? bias),
      size: new THREE.Vector3(size[0], size[1], size[2]),
    }))
  }

  return {
    wall: {
      lights: makeLights([1.45, 1.12, 0.88]),
      lightsInfo: new THREE.Vector4(-1.55, 1.35, 0.26, 0),
    } satisfies LightGroupConfig,
    floor: {
      lights: makeLights([1.1, 0.8, 0.72]),
      lightsInfo: new THREE.Vector4(-1.3, 1.1, 0.2, 0),
    } satisfies LightGroupConfig,
    object: {
      lights: makeLights([1.22, 0.95, 0.82]),
      lightsInfo: new THREE.Vector4(-1.4, 1.18, 0.24, 0),
    } satisfies LightGroupConfig,
  }
}

/**
 * GLB 命名并不等于业务设备。这里先按“墙 / 楼板 / 家具 / 深色设备 / 车辆 / 信息牌”分层，
 * 只替换真正影响气质的材质，其余保留原材质并做定向增强。
 */
function applyShowroomMaterials(
  scene: THREE.Group,
  floorId: FloorId,
  shaderMats: ReturnType<typeof useShaderMaterials>,
  floorLightUnis: THREE.Vector4[],
) {
  const floorConfig = showroomVisualConfig.floors[floorId]
  const lightGroups = createLightGroups(floorId, floorLightUnis)
  const aoMap = extractAOMap(scene)

  scene.traverse((obj) => {
    if (!(obj instanceof THREE.Mesh)) return

    const sourceMaterial = Array.isArray(obj.material) ? obj.material[0] : obj.material
    const materialName = sourceMaterial?.name ?? ''
    const nodeName = obj.name
    const lowerNodeName = nodeName.toLowerCase()

    if (lowerNodeName.includes('visualcone') || lowerNodeName.startsWith('effect')) {
      obj.visible = false
      return
    }

    const role = classifyRole(nodeName, materialName)
    obj.userData.materialRole = role

    if (role === 'wallGlass') {
      obj.material = shaderMats.createWallGlassMaterial(lightGroups.wall, {
        color: new THREE.Color(showroomVisualConfig.materialPalette.wallGlass),
        opacity: 1,
        envIntensity: floorConfig.envBias,
      })
    } else if (role === 'floorDeck') {
      obj.material = shaderMats.createFloorDeckMaterial(lightGroups.floor, {
        color: new THREE.Color(showroomVisualConfig.materialPalette.floorDeck),
        envIntensity: floorConfig.envBias,
      })
      obj.renderOrder = 2
    } else if (role === 'furniture') {
      obj.material = shaderMats.createFurnitureMaterial(lightGroups.object, {
        aoMap: aoMap ?? undefined,
        envIntensity: floorConfig.envBias,
      })
    } else if (role === 'applianceMetal') {
      obj.material = shaderMats.createApplianceMaterial(sourceMaterial, {
        envIntensity: 1.24,
      })
    } else if (role === 'vehicleFx') {
      obj.material = shaderMats.createVehicleMaterial(sourceMaterial)
    } else if (role === 'signage') {
      obj.material = shaderMats.createSignageMaterial(sourceMaterial)
    }

    const deviceId = resolveSceneDeviceId(floorId, nodeName)
    if (deviceId) {
      selectableMeshes.set(obj, deviceId)
      floorForSelectable.set(obj, floorId)
    }
  })
}

function createReflectionGroup(scene: THREE.Group, shaderMats: ReturnType<typeof useShaderMaterials>) {
  const reflection = scene.clone(true)
  reflection.scale.set(1.015, -0.22, 1.015)

  reflection.traverse((obj) => {
    if (!(obj instanceof THREE.Mesh)) return
    const role = (obj.userData.materialRole ?? 'reflection') as ShowroomMaterialRole
    obj.material = shaderMats.createReflectionMaterial(role)
    obj.renderOrder = 0
  })

  return reflection
}

function getReflectionY(sourceY: number) {
  return showroomVisualConfig.ground.planeY - Math.min(sourceY * 0.05, 0.74)
}

function moveFloorPair(floorId: FloorId, targetY: number, duration: number) {
  const floor = floorRefs[floorId].value
  const reflection = reflectionRefs[floorId].value
  if (floor) {
    gsap.to(floor.position, { y: targetY, duration, ease: 'cubic.out' })
  }
  if (reflection) {
    gsap.to(reflection.position, { y: getReflectionY(targetY), duration, ease: 'cubic.out' })
  }
}

function buildFloorDevices(floorId: FloorId) {
  return Object.entries(worldStore.devices).filter(([, device]) => {
    return getFloorForDevice(device.id, device.location.room) === floorId
  })
}

function renderFloorLabel(floorId: FloorId) {
  const element = labelElements.get(floorId)
  if (!element) return

  const devices = buildFloorDevices(floorId)
  const activeFloor = uiStore.activeFloor
  const visibleInOverview = false
  const visibleInFocus = activeFloor === floorId
  element.style.display = activeFloor === 'overview'
    ? (visibleInOverview ? 'block' : 'none')
    : (visibleInFocus ? 'block' : 'none')

  const chipsHtml = devices.length > 0
    ? devices.map(([deviceId, device]) => {
        const activeClass = uiStore.activeDevice === deviceId ? 'active' : ''
        const liveClass = device.state.power ? 'live' : ''
        return `<button class="scene-floor-label__chip ${activeClass} ${liveClass}" data-device-id="${deviceId}" data-floor-id="${floorId}">${device.type}</button>`
      }).join('')
    : '<span class="scene-floor-label__sub">当前没有接入设备</span>'

  element.innerHTML = `
    <p class="scene-floor-label__title">${FLOOR_DISPLAY_NAMES[floorId]}</p>
    <p class="scene-floor-label__sub">${devices.length} 个在线入口，可直接打开右侧控制器。</p>
    <div class="scene-floor-label__actions">${chipsHtml}</div>
  `

  element.querySelectorAll<HTMLButtonElement>('[data-device-id]').forEach((button) => {
    button.addEventListener('click', () => {
      const deviceId = button.dataset.deviceId
      const floor = button.dataset.floorId as FloorId | undefined
      if (!deviceId || !floor) return
      uiStore.setActiveFloor(floor)
      uiStore.setActiveDevice(deviceId)
    })
  })
}

function attachFloorLabel(floorId: FloorId, scene: THREE.Group) {
  const element = document.createElement('div')
  element.className = 'scene-floor-label'
  labelElements.set(floorId, element)
  renderFloorLabel(floorId)

  const labelObject = new CSS2DObject(element)
  const anchor = showroomVisualConfig.floors[floorId].labelAnchor
  labelObject.position.set(anchor[0], anchor[1], anchor[2])
  scene.add(labelObject)
}

function refreshFloorLabels() {
  floorOrder.forEach((floorId) => renderFloorLabel(floorId))
}

function createLabelRenderer() {
  if (!sceneHostEl.value) return

  labelRenderer = new CSS2DRenderer()
  labelRenderer.setSize(sceneHostEl.value.clientWidth, sceneHostEl.value.clientHeight)
  labelRenderer.domElement.className = 'scene-label-layer'
  labelRenderer.domElement.style.pointerEvents = 'none'
  sceneHostEl.value.appendChild(labelRenderer.domElement)
  showroomRuntime.labelRenderer.value = labelRenderer
}

function resizeLabelRenderer() {
  if (!sceneHostEl.value || !labelRenderer) return
  labelRenderer.setSize(sceneHostEl.value.clientWidth, sceneHostEl.value.clientHeight)
}

function pickSceneDevice(event: PointerEvent) {
  const activeCamera = showroomRuntime.camera.value
  if (!activeCamera || !canvasEl || selectableMeshes.size === 0) return

  const rect = canvasEl.getBoundingClientRect()
  pointer.x = ((event.clientX - rect.left) / rect.width) * 2 - 1
  pointer.y = -((event.clientY - rect.top) / rect.height) * 2 + 1

  raycaster.setFromCamera(pointer, activeCamera)
  const intersects = raycaster.intersectObjects([...selectableMeshes.keys()], true)
  const picked = intersects.find((entry) => selectableMeshes.has(entry.object))
  if (!picked) return

  const deviceId = selectableMeshes.get(picked.object)
  const floorId = floorForSelectable.get(picked.object)
  if (!deviceId || !floorId) return

  uiStore.setActiveFloor(floorId)
  uiStore.setActiveDevice(deviceId)
}

watch(
  () => uiStore.activeFloor,
  (floorId) => {
    const preset = cameraPresets[floorId as keyof typeof cameraPresets] ?? cameraPresets.overview

    if (floorId === 'overview') {
      floorsExpanded = false
      moveFloorPair('F1', showroomVisualConfig.floors.F1.collapsedY, 1.0)
      moveFloorPair('F2', showroomVisualConfig.floors.F2.collapsedY, 1.0)
      moveFloorPair('F3', showroomVisualConfig.floors.F3.collapsedY, 1.0)
      camera.animateTo(preset, 1.0)
    } else {
      if (!floorsExpanded) {
        floorsExpanded = true
      }
      moveFloorPair('F1', showroomVisualConfig.floors.F1.expandedY, 0.86)
      moveFloorPair('F2', showroomVisualConfig.floors.F2.expandedY, 0.86)
      moveFloorPair('F3', showroomVisualConfig.floors.F3.expandedY, 0.86)
      camera.animateTo(preset, 0.82)
    }

    refreshFloorLabels()
  },
  { immediate: true },
)

watch(
  () => JSON.stringify(worldStore.devices),
  () => {
    refreshFloorLabels()
  },
)

watch(
  () => uiStore.activeDevice,
  () => {
    refreshFloorLabels()
  },
)

onMounted(async () => {
  try {
    uiStore.setSceneLoadStatus('loading')
    createLabelRenderer()

    const [matcapRoughness, matcapReflection] = await Promise.all([
      loadTexture('/textures/matcap_roughness_3.webp'),
      loadTexture('/textures/matcap_reflection.webp'),
    ])

    const shaderMats = useShaderMaterials({ matcapRoughness, matcapReflection })

    const hdrTexture = await new Promise<THREE.Texture>((resolve, reject) => {
      new HDRLoader().load('/textures/roomhdr_blue.hdr', resolve, undefined, reject)
    })
    hdrTexture.mapping = THREE.EquirectangularReflectionMapping
    showroomRuntime.environment.value = hdrTexture

    for (const floorId of floorOrder) {
      const floorConfig = showroomVisualConfig.floors[floorId]
      const scene = await loadGLB(`/models/${floorId}.glb`)
      scene.position.set(0, floorConfig.collapsedY, 0)

      const floorLightUnis = lightUniforms.initFloor({
        floorId,
        numLights: floorConfig.lights.length,
        positions: floorConfig.lights,
        floorY: floorConfig.collapsedY,
      })

      applyShowroomMaterials(scene, floorId, shaderMats, floorLightUnis)
      registerDeviceNodes(floorId, scene)

      const reflection = createReflectionGroup(scene, shaderMats)
      reflection.position.set(0, getReflectionY(floorConfig.collapsedY), 0)

      attachFloorLabel(floorId, scene)

      floorRefs[floorId].value = scene
      reflectionRefs[floorId].value = reflection
    }

    showroomRuntime.onFrame.value = (dt, elapsed) => {
      shaderMats.updateShowroomEffects(dt, elapsed)
      groundMaterial.uniforms.u_time.value = elapsed
    }

    setupLightWatchers()
    resizeLabelRenderer()
    uiStore.setSceneLoadStatus('loaded')
  } catch (error) {
    console.error('Scene load error:', error)
    uiStore.setSceneLoadStatus('error')
  }

  setTimeout(() => {
    canvasEl = sceneHostEl.value?.querySelector('canvas') ?? null
    if (!canvasEl) return

    const handlePointerDown = (event: PointerEvent) => {
      pointerDown = { x: event.clientX, y: event.clientY }
      camera.onPointerDown(event)
    }

    const handlePointerMove = (event: PointerEvent) => {
      camera.onPointerMove(event)
    }

    const handlePointerUp = (event: PointerEvent) => {
      const moved = Math.hypot(event.clientX - pointerDown.x, event.clientY - pointerDown.y)
      camera.onPointerUp()
      if (moved < 6) {
        pickSceneDevice(event)
      }
    }

    canvasEl.addEventListener('pointerdown', handlePointerDown)
    canvasEl.addEventListener('pointermove', handlePointerMove)
    canvasEl.addEventListener('pointerup', handlePointerUp)
    canvasEl.addEventListener('pointerleave', camera.onPointerUp)
    canvasEl.addEventListener('wheel', camera.onWheel, { passive: true })

    ;(canvasEl as HTMLCanvasElement & {
      __showroomHandlers?: {
        down: (event: PointerEvent) => void
        move: (event: PointerEvent) => void
        up: (event: PointerEvent) => void
      }
    }).__showroomHandlers = {
      down: handlePointerDown,
      move: handlePointerMove,
      up: handlePointerUp,
    }
  }, 120)

  window.addEventListener('resize', resizeLabelRenderer)
})

onBeforeUnmount(() => {
  if (canvasEl) {
    const handlers = (canvasEl as HTMLCanvasElement & {
      __showroomHandlers?: {
        down: (event: PointerEvent) => void
        move: (event: PointerEvent) => void
        up: (event: PointerEvent) => void
      }
    }).__showroomHandlers

    if (handlers) {
      canvasEl.removeEventListener('pointerdown', handlers.down)
      canvasEl.removeEventListener('pointermove', handlers.move)
      canvasEl.removeEventListener('pointerup', handlers.up)
    }
    canvasEl.removeEventListener('pointerleave', camera.onPointerUp)
    canvasEl.removeEventListener('wheel', camera.onWheel)
  }

  floorOrder.forEach((floorId) => {
    if (floorRefs[floorId].value) gsap.killTweensOf(floorRefs[floorId].value.position)
    if (reflectionRefs[floorId].value) gsap.killTweensOf(reflectionRefs[floorId].value.position)
  })

  window.removeEventListener('resize', resizeLabelRenderer)
  showroomRuntime.environment.value = null
  showroomRuntime.onFrame.value = null
  showroomRuntime.camera.value = null
  showroomRuntime.scene.value = null
  showroomRuntime.labelRenderer.value = null
  if (labelRenderer?.domElement.parentNode) {
    labelRenderer.domElement.parentNode.removeChild(labelRenderer.domElement)
  }
})
</script>

<template>
  <div ref="sceneHostEl" class="scene-container">
    <TresCanvas
      clear-color="#090c12"
      :antialias="true"
      :tone-mapping="1"
      :tone-mapping-exposure="0.94"
    >
      <TresPerspectiveCamera
        :position="[-48, 60, 80]"
        :fov="12"
        :near="0.5"
        :far="320"
      />

      <TresAmbientLight :intensity="0.028" color="#dce4ee" />

      <SceneRenderLoop />

      <primitive v-if="reflectionRefs.F1.value" :object="reflectionRefs.F1.value" />
      <primitive v-if="reflectionRefs.F2.value" :object="reflectionRefs.F2.value" />
      <primitive v-if="reflectionRefs.F3.value" :object="reflectionRefs.F3.value" />

      <TresMesh :position="[0, showroomVisualConfig.ground.planeY, 0]" :rotation-x="-Math.PI / 2">
        <TresPlaneGeometry :args="[showroomVisualConfig.ground.size, showroomVisualConfig.ground.size]" />
        <primitive :object="groundMaterial" attach="material" />
      </TresMesh>

      <primitive v-if="floorRefs.F1.value" :object="floorRefs.F1.value" />
      <primitive v-if="floorRefs.F2.value" :object="floorRefs.F2.value" />
      <primitive v-if="floorRefs.F3.value" :object="floorRefs.F3.value" />
    </TresCanvas>
  </div>
</template>


<style scoped>
.scene-container {
  position: absolute;
  inset: 0;
}
</style>
