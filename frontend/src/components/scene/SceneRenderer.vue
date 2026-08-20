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
  getFloorLightCurrent,
  initDeviceAnimStore,
  getSceneNodesForNames,
  registerDeviceNodes,
  setupLightWatchers,
} from '@/composables/useDeviceAnimations'
import { showroomVisualConfig, type ShowroomMaterialRole } from '@/config/showroomVisualConfig'
import {
  getSceneBindings,
  getSelectableBindingNodes,
} from '@/utils/sceneBindings'
import { buildShowroomLightEntries } from '@/utils/showroomLightGroups'
import {
  getDeviceLabel,
  getFloorForDevice,
  isDeviceOnline,
} from '@/utils/deviceFloorMap'
import { showSceneFloorLabels } from '@/config/sceneOverlayConfig'
import { showroomRuntime } from './showroomRuntime'
import { SceneRenderLoop } from './SceneRenderLoop'
import groundVert from '@/shaders/ground/vertex.glsl?raw'
import groundFrag from '@/shaders/ground/fragment.glsl?raw'

type FloorId = 'F1' | 'F2' | 'F3'
type RoomFeedbackEntry = {
  halo: THREE.Mesh
  occupancyHalo: THREE.Mesh
  pointLight: THREE.PointLight
}

const uiStore = useUIStore()
const worldStore = useWorldStore()
const camera = useSphericalCamera()
const lightUniforms = useLightUniforms()
initDeviceAnimStore(worldStore, uiStore)

const sceneHostEl = ref<HTMLElement | null>(null)
const glbLoader = new GLTFLoader()
const textureLoader = new THREE.TextureLoader()
const raycaster = new THREE.Raycaster()
const pointer = new THREE.Vector2()
const ambientLightIntensity = ref(0.028)
const ambientLightColor = ref('#dce4ee')

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

const lightSourceRefs: Record<FloorId, ReturnType<typeof shallowRef<THREE.Group | null>>> = {
  F1: shallowRef<THREE.Group | null>(null),
  F2: shallowRef<THREE.Group | null>(null),
  F3: shallowRef<THREE.Group | null>(null),
}

const labelElements = new Map<FloorId, HTMLDivElement>()
const selectableMeshes = new Map<THREE.Object3D, string>()
const floorForSelectable = new Map<THREE.Object3D, FloorId>()
const roomFeedbackRefs = new Map<FloorId, Map<string, RoomFeedbackEntry>>()

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

function createProceduralFloorPlaceholder(floorId: FloorId): THREE.Group {
  // S5-T7: GLB 加载失败时的纯色方块占位——确保系统在无 gamemcu 资产时仍能跑通
  const group = new THREE.Group()
  group.name = `procedural_${floorId}`

  const geo = new THREE.BoxGeometry(12, 0.3, 16)
  const mat = new THREE.MeshStandardMaterial({ color: 0x3a3a5c, roughness: 0.7 })
  const floor = new THREE.Mesh(geo, mat)
  floor.name = `floor_${floorId}`
  floor.position.set(0, 0, 0)
  group.add(floor)

  const wallGeo = new THREE.BoxGeometry(12, 4, 0.2)
  const wallMat = new THREE.MeshStandardMaterial({ color: 0x8bafb1, roughness: 0.5, transparent: true, opacity: 0.35 })
  const positions: [number, number, number][] = [
    [0, 2, -8], [0, 2, 8], [-6, 2, 0], [6, 2, 0],
  ]
  for (const [x, y, z] of positions) {
    const wall = new THREE.Mesh(wallGeo, wallMat)
    wall.name = `wall_${floorId}`
    wall.position.set(x, y, z)
    if (z === 0) wall.rotation.y = Math.PI / 2
    group.add(wall)
  }

  return group
}

async function loadFloorScene(floorId: FloorId): Promise<THREE.Group> {
  try {
    return await loadGLB(`/models/${floorId}.glb`)
  } catch (err) {
    console.warn(`GLB ${floorId}.glb 加载失败，使用程序化占位：`, err)
    return createProceduralFloorPlaceholder(floorId)
  }
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

function createLightGroups(floorId: FloorId, uniforms: THREE.Vector4[]) {
  const floor = showroomVisualConfig.floors[floorId]
  const bias = floor.lightBias
  const volumeScale = floor.lightVolumeScale ?? 1
  const makeLights = (size: [number, number, number]) => {
    return buildShowroomLightEntries(
      floor.lights,
      uniforms,
      size,
      volumeScale,
      bias,
    )
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

function createLightSourceGroup(floorId: FloorId) {
  const floorConfig = showroomVisualConfig.floors[floorId]
  const group = new THREE.Group()
  group.name = `showroom-light-sources-${floorId}`

  for (const [x, , z] of floorConfig.lights) {
    const halo = new THREE.Mesh(
      new THREE.CircleGeometry(floorConfig.lightSourceSize, 32),
      new THREE.MeshBasicMaterial({
        color: new THREE.Color(0xdce7ff),
        transparent: true,
        opacity: 0,
        depthWrite: false,
        blending: THREE.AdditiveBlending,
        side: THREE.DoubleSide,
        toneMapped: false,
      }),
    )
    halo.rotation.x = -Math.PI / 2
    halo.renderOrder = 9

    const core = new THREE.Mesh(
      new THREE.CircleGeometry(floorConfig.lightSourceSize * 0.42, 24),
      new THREE.MeshBasicMaterial({
        color: new THREE.Color(0xf9fbff),
        transparent: true,
        opacity: 0,
        depthWrite: false,
        blending: THREE.AdditiveBlending,
        side: THREE.DoubleSide,
        toneMapped: false,
      }),
    )
    core.position.y = 0.01
    core.rotation.x = -Math.PI / 2
    core.renderOrder = 10

    const source = new THREE.Group()
    source.position.set(x, floorConfig.lightSourceY, z)
    source.userData.baseScale = 1
    source.add(halo, core)
    group.add(source)
  }

  return group
}

function updateLightSourceGroup(floorId: FloorId, dt: number) {
  const group = lightSourceRefs[floorId].value
  if (!group) return

  const gain = floorId === 'F1' ? 1 : floorId === 'F2' ? 1.15 : 1.2
  const current = getFloorLightCurrent(floorId)
  const normalized = THREE.MathUtils.clamp((current - 0.05) / Math.max(gain - 0.05, 0.01), 0, 1)
  const time = Date.now() * 0.0012

  for (const source of group.children) {
    const pulse = 0.97 + Math.sin(source.position.x + time) * 0.015
    source.scale.setScalar(THREE.MathUtils.damp(source.scale.x, 0.88 + normalized * 0.1 * pulse, 6, dt))
    source.visible = normalized > 0.02

    for (const child of source.children) {
      if (!(child instanceof THREE.Mesh)) continue
      const material = child.material
      if (!(material instanceof THREE.MeshBasicMaterial)) continue
      const targetOpacity = child === source.children[0]
        ? normalized * 0.14
        : normalized * 0.8
      material.opacity = THREE.MathUtils.damp(material.opacity, targetOpacity, 8, dt)
    }
  }
}

function parseTimeHour(timeOfDay: string) {
  const [hours, minutes] = timeOfDay.split(':').map((value) => Number(value))
  return hours + minutes / 60
}

function createRoomFeedbackGroup(floorId: FloorId) {
  const group = new THREE.Group()
  group.name = `showroom-room-feedback-${floorId}`

  const entries = new Map<string, RoomFeedbackEntry>()
  const anchors = showroomVisualConfig.floors[floorId].roomAnchors

  for (const [roomId, anchor] of Object.entries(anchors)) {
    const halo = new THREE.Mesh(
      new THREE.CircleGeometry(2.3, 40),
      new THREE.MeshBasicMaterial({
        color: new THREE.Color(0xffd56c),
        transparent: true,
        opacity: 0,
        depthWrite: false,
        blending: THREE.AdditiveBlending,
        side: THREE.DoubleSide,
        toneMapped: false,
      }),
    )
    halo.rotation.x = -Math.PI / 2
    halo.position.set(anchor[0], anchor[1], anchor[2])
    halo.renderOrder = 6

    const occupancyHalo = new THREE.Mesh(
      new THREE.RingGeometry(2.45, 3.2, 48),
      new THREE.MeshBasicMaterial({
        color: new THREE.Color(0x8dc8ff),
        transparent: true,
        opacity: 0,
        depthWrite: false,
        blending: THREE.AdditiveBlending,
        side: THREE.DoubleSide,
        toneMapped: false,
      }),
    )
    occupancyHalo.rotation.x = -Math.PI / 2
    occupancyHalo.position.set(anchor[0], anchor[1] + 0.01, anchor[2])
    occupancyHalo.renderOrder = 7

    const pointLight = new THREE.PointLight(0xffe0a3, 0, 10, 2.2)
    pointLight.position.set(anchor[0], anchor[1] + 1.8, anchor[2])

    group.add(halo, occupancyHalo, pointLight)
    entries.set(roomId, { halo, occupancyHalo, pointLight })
  }

  roomFeedbackRefs.set(floorId, entries)
  return group
}

function updateEnvironmentLook(dt: number) {
  const hour = parseTimeHour(worldStore.environment.time_of_day)
  const weather = worldStore.environment.weather
  const dayFactor = THREE.MathUtils.clamp(Math.sin(((hour - 6) / 12) * Math.PI), 0, 1)
  const eveningFactor = THREE.MathUtils.clamp(1 - Math.abs(hour - 19) / 4, 0, 1)
  const rainyFactor = weather === 'rainy' ? 1 : weather === 'cloudy' ? 0.45 : 0

  const targetAmbient = THREE.MathUtils.lerp(0.016, 0.05, dayFactor) - rainyFactor * 0.008
  ambientLightIntensity.value = THREE.MathUtils.damp(
    ambientLightIntensity.value,
    targetAmbient,
    3.6,
    dt,
  )

  const ambientColorObj = new THREE.Color(0x9cb6d3)
  ambientColorObj.lerp(new THREE.Color(0xffd7a1), eveningFactor * 0.5)
  ambientColorObj.lerp(new THREE.Color(0x7f93aa), rainyFactor * 0.45)
  ambientLightColor.value = `#${ambientColorObj.getHexString()}`

  const centerColor = new THREE.Color(0x0d1117)
  centerColor.lerp(new THREE.Color(0x18202a), dayFactor * 0.55)
  centerColor.lerp(new THREE.Color(0x101722), rainyFactor * 0.35)
  groundMaterial.uniforms.u_centerColor.value.lerp(centerColor, Math.min(3.4 * dt, 1))

  const reflectionColor = new THREE.Color(0x5f7487)
  reflectionColor.lerp(new THREE.Color(0xd4b37b), eveningFactor * 0.4)
  reflectionColor.lerp(new THREE.Color(0x7d8fa3), rainyFactor * 0.35)
  groundMaterial.uniforms.u_reflectionColor.value.lerp(reflectionColor, Math.min(2.8 * dt, 1))
}

function updateRoomFeedback(floorId: FloorId, dt: number) {
  const entries = roomFeedbackRefs.get(floorId)
  if (!entries) return

  const hour = parseTimeHour(worldStore.environment.time_of_day)
  const weather = worldStore.environment.weather
  const coolDay = hour >= 7 && hour < 17
  const baseLightColor = new THREE.Color(coolDay ? 0xb7d7ff : 0xffd8a0)
  if (weather === 'rainy') {
    baseLightColor.lerp(new THREE.Color(0x8db3d8), 0.45)
  }

  for (const [roomId, entry] of entries) {
    const room = worldStore.rooms[roomId]
    if (!room) continue

    const lightLevel = THREE.MathUtils.clamp((room.light_level ?? 0) / 520, 0, 1)
    const occupancyBoost = room.occupancy ? 1 : 0
    const localLightTarget = lightLevel * 1.6 + occupancyBoost * 0.28
    const haloTarget = lightLevel * 0.18 + occupancyBoost * 0.08
    const occupancyTarget = room.occupancy ? 0.16 + Math.sin(Date.now() * 0.002 + entry.pointLight.position.x) * 0.02 : 0

    const haloMat = entry.halo.material as THREE.MeshBasicMaterial
    haloMat.color.lerp(baseLightColor, Math.min(4.2 * dt, 1))
    haloMat.opacity = THREE.MathUtils.damp(haloMat.opacity, haloTarget, 4.5, dt)

    const occupancyMat = entry.occupancyHalo.material as THREE.MeshBasicMaterial
    occupancyMat.opacity = THREE.MathUtils.damp(occupancyMat.opacity, Math.max(occupancyTarget, 0), 4.5, dt)

    entry.pointLight.color.lerp(baseLightColor, Math.min(4.2 * dt, 1))
    entry.pointLight.intensity = THREE.MathUtils.damp(entry.pointLight.intensity, localLightTarget, 4.2, dt)
  }
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
    return getFloorForDevice(device.id, device.location.room, device.floor_id) === floorId
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
        const liveClass = isDeviceOnline(device) ? 'live' : ''
        return `<button class="scene-floor-label__chip ${activeClass} ${liveClass}" data-device-id="${deviceId}" data-floor-id="${floorId}">${getDeviceLabel(device, deviceId)}</button>`
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
  if (!showSceneFloorLabels) return

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
  if (!showSceneFloorLabels) return
  floorOrder.forEach((floorId) => renderFloorLabel(floorId))
}

/**
 * 交互命中改为读取注册表里的 scene_bindings。
 * 这样设备扩容后，点击层不会继续卡死在少量硬编码节点上。
 */
function refreshSelectableMeshes() {
  selectableMeshes.clear()
  floorForSelectable.clear()

  for (const [deviceId, device] of Object.entries(worldStore.devices)) {
    const floorId = getFloorForDevice(deviceId, device.location.room, device.floor_id)
    if (!floorId || !floorOrder.includes(floorId as FloorId)) continue

    const bindings = getSceneBindings(device)
    const nodeNames = getSelectableBindingNodes(bindings)
    if (nodeNames.length === 0) continue

    const nodes = getSceneNodesForNames(floorId, nodeNames)
    for (const node of nodes) {
      selectableMeshes.set(node, deviceId)
      floorForSelectable.set(node, floorId as FloorId)
    }
  }
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
    refreshSelectableMeshes()
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
    if (showSceneFloorLabels) {
      createLabelRenderer()
    }

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
      const scene = await loadFloorScene(floorId)
      scene.position.set(0, floorConfig.collapsedY, 0)

      const floorLightUnis = lightUniforms.initFloor({
        floorId,
        numLights: floorConfig.lights.length,
        positions: floorConfig.lights,
        floorY: floorConfig.collapsedY,
      })

      applyShowroomMaterials(scene, floorId, shaderMats, floorLightUnis)
      registerDeviceNodes(floorId, scene)
      refreshSelectableMeshes()

      const reflection = createReflectionGroup(scene, shaderMats)
      reflection.position.set(0, getReflectionY(floorConfig.collapsedY), 0)
      const lightSources = createLightSourceGroup(floorId)
      scene.add(lightSources)
      scene.add(createRoomFeedbackGroup(floorId))

      attachFloorLabel(floorId, scene)

      floorRefs[floorId].value = scene
      reflectionRefs[floorId].value = reflection
      lightSourceRefs[floorId].value = lightSources
    }

    showroomRuntime.onFrame.value = (dt, elapsed) => {
      updateEnvironmentLook(dt)
      for (const floorId of floorOrder) {
        const floorScene = floorRefs[floorId].value
        if (floorScene) {
          lightUniforms.setFloorTransform(floorId, floorScene.position)
        }
        updateLightSourceGroup(floorId, dt)
        updateRoomFeedback(floorId, dt)
      }
      shaderMats.updateShowroomEffects(dt, elapsed)
      groundMaterial.uniforms.u_time.value = elapsed
    }

    setupLightWatchers()
    if (showSceneFloorLabels) {
      resizeLabelRenderer()
    }
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

  if (showSceneFloorLabels) {
    window.addEventListener('resize', resizeLabelRenderer)
  }
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

  if (showSceneFloorLabels) {
    window.removeEventListener('resize', resizeLabelRenderer)
  }
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

      <TresAmbientLight :intensity="ambientLightIntensity" :color="ambientLightColor" />

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
