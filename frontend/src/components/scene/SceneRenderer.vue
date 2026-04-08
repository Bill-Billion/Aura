<script setup lang="ts">
import { ref, onMounted, onBeforeUnmount, shallowRef, watch } from 'vue'
import { TresCanvas } from '@tresjs/core'
import * as THREE from 'three'

// Disable color management to match gamemcu's Three.js r175 behavior
// Without this, Color(hex) auto-converts sRGB→linear, breaking our color values
THREE.ColorManagement.enabled = false
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js'
import { RGBELoader } from 'three/addons/loaders/RGBELoader.js'
import gsap from 'gsap'
import { useUIStore } from '@/stores/uiStore'
import { useSphericalCamera } from '@/composables/useSphericalCamera'
import { useShaderMaterials } from '@/composables/useShaderMaterials'
import { useLightUniforms } from '@/composables/useLightUniforms'
import { registerDeviceNodes, setupLightWatchers, updateDeviceAnimations, initDeviceAnimStore } from '@/composables/useDeviceAnimations'
import groundVert from '@/shaders/ground/vertex.glsl?raw'
import groundFrag from '@/shaders/ground/fragment.glsl?raw'

const uiStore = useUIStore()
const camera = useSphericalCamera()
const lightUniforms = useLightUniforms()

// Init device animation with worldStore reference
import { useWorldStore } from '@/stores/worldStore'
initDeviceAnimStore(useWorldStore())

const f1 = shallowRef<THREE.Group | null>(null)
const f2 = shallowRef<THREE.Group | null>(null)
const f3 = shallowRef<THREE.Group | null>(null)

const glbLoader = new GLTFLoader()
const textureLoader = new THREE.TextureLoader()

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

// ======= Background sphere (gamemcu: uniform dark blue-gray #2d3040) =======
const bgSphereMaterial = new THREE.MeshBasicMaterial({
  color: new THREE.Color(0x1e242d),
  side: THREE.BackSide,
  depthWrite: false,
  depthTest: false,  // render behind everything
  fog: false,
})

// ======= Ground plane material =======
const groundMaterial = new THREE.ShaderMaterial({
  vertexShader: groundVert,
  fragmentShader: groundFrag,
  uniforms: {
    u_centerColor: { value: new THREE.Color(0x222830) },
    u_edgeColor: { value: new THREE.Color(0x1e242d) },
    u_center: { value: new THREE.Vector2(0.48, 0.45) },
    u_radius: { value: 0.55 },
  },
  toneMapped: false, // prevent ACES from crushing dark values
})

// ======= Floor configs =======
const FLOOR_CONFIGS = [
  {
    id: 'F1', path: '/models/F1.glb', collapsedY: 1, expandedY: 0,
    numLights: 3,
    lights: [[-2, 0, -1.5], [-2, 0, 1], [-1, 0, 4]] as [number,number,number][],
  },
  {
    id: 'F2', path: '/models/F2.glb', collapsedY: 7, expandedY: 18,
    numLights: 2,
    lights: [[-4, 0, -1.5], [3, 0, -2.5]] as [number,number,number][],
  },
  {
    id: 'F3', path: '/models/F3.glb', collapsedY: 13, expandedY: 35,
    numLights: 2,
    lights: [[-1, 0, 3], [3, 0, -2.5]] as [number,number,number][],
  },
]

const CAMERA_PRESETS = {
  overview: { springLength: 120, lookAt: [-1.5, 7.5, 0.5] as [number,number,number], theta: 2.38 + Math.PI, phi: 1.25, fov: 12 },
  F1: { springLength: 50, lookAt: [0, 2.5, 0] as [number,number,number], theta: 2.35 + Math.PI, phi: 0.84, fov: 12, smoothing: 4, rotateSmoothing: 4 },
  F2: { springLength: 50, lookAt: [0, 19.5, 0] as [number,number,number], theta: 2.35 + Math.PI, phi: 0.84, fov: 12, smoothing: 4, rotateSmoothing: 4 },
  F3: { springLength: 50, lookAt: [0, 36.5, 0] as [number,number,number], theta: 2.35 + Math.PI, phi: 0.84, fov: 12, smoothing: 4, rotateSmoothing: 4 },
}

// ======= Apply 3 shader classes based on mesh.name =======
function applyShaderMaterials(
  scene: THREE.Group,
  floorConfig: typeof FLOOR_CONFIGS[0],
  shaderMats: ReturnType<typeof useShaderMaterials>,
  floorLightUnis: THREE.Vector4[],
  envMap?: THREE.Texture | null,
) {
  // 3 different lightGroup configs per gamemcu source
  const wallLightGroup = {
    lights: floorConfig.lights.map((pos, i) => ({
      position: new THREE.Vector4(pos[0], pos[1], pos[2], floorLightUnis[i]?.w ?? 1.0),
      size: new THREE.Vector3(3, 2, 1.5),
    })),
    lightsInfo: new THREE.Vector4(-2, 2, 0.2, 0),
  }
  const objectLightGroup = {
    lights: floorConfig.lights.map((pos, i) => ({
      position: new THREE.Vector4(pos[0], pos[1], pos[2], floorLightUnis[i]?.w ?? 1.0),
      size: new THREE.Vector3(3, 2, 1.5),
    })),
    lightsInfo: new THREE.Vector4(-2.5, 2.5, 0.2, 0),
  }
  const floorLightGroup = {
    lights: floorConfig.lights.map((pos, i) => ({
      position: new THREE.Vector4(pos[0], pos[1], pos[2], floorLightUnis[i]?.w ?? 1.0),
      size: new THREE.Vector3(1.5, 1.8, 1),
    })),
    lightsInfo: new THREE.Vector4(-3, 2.5, 0.4, 0),
  }

  // Extract AO texture from GLB
  const aoMap = extractAOMap(scene)

  scene.traverse((obj) => {
    if (!(obj instanceof THREE.Mesh)) return
    const mat = obj.material as THREE.MeshStandardMaterial
    const matName = mat?.name?.toLowerCase() ?? ''
    const nodeName = obj.name.toLowerCase()

    if (nodeName.includes('visualcone') || nodeName.includes('effect')) {
      obj.visible = false
      return
    }

    if (nodeName === 'wall') {
      // I3 class: walls — Fresnel transparency + SDF
      obj.material = shaderMats.createI3ClassMaterial(wallLightGroup)
    } else if (nodeName === 'floor') {
      // z1 class: indoor floor — higher matcap min, semi-transparent
      obj.material = shaderMats.createZ1ClassMaterial(floorLightGroup)
    } else if (nodeName === 'car') {
      // Car: keep original GLB material
      // (gamemcu uses onBeforeCompile for sweep effect, skip for now)
    } else {
      // K class: all other objects (furniture, fixtures, appliances)
      obj.material = shaderMats.createKClassMaterial(objectLightGroup, {
        aoMap: aoMap ?? undefined,
      })
    }
  })
}

// Extract first texture from GLB as AO map
function extractAOMap(scene: THREE.Group): THREE.Texture | null {
  let aoMap: THREE.Texture | null = null
  scene.traverse((obj) => {
    if (aoMap) return
    if (!(obj instanceof THREE.Mesh)) return
    const mat = obj.material as THREE.MeshStandardMaterial
    if (mat?.aoMap) {
      aoMap = mat.aoMap
    }
  })
  return aoMap
}

// ======= Floor expansion =======
let floorsExpanded = false

watch(() => uiStore.activeFloor, (floorId) => {
  const preset = CAMERA_PRESETS[floorId as keyof typeof CAMERA_PRESETS] ?? CAMERA_PRESETS.overview

  if (floorId === 'overview') {
    floorsExpanded = false
    if (f1.value) { f1.value.visible = true; gsap.to(f1.value.position, { y: 1, duration: 1.0, ease: 'cubic.out' }) }
    if (f2.value) { f2.value.visible = true; gsap.to(f2.value.position, { y: 7, duration: 1.0, ease: 'cubic.out' }) }
    if (f3.value) { f3.value.visible = true; gsap.to(f3.value.position, { y: 13, duration: 1.0, ease: 'cubic.out' }) }
    camera.animateTo(preset, 1.0)
  } else {
    if (!floorsExpanded) {
      floorsExpanded = true
      if (f1.value) { f1.value.visible = true; gsap.to(f1.value.position, { y: 0, duration: 0.8, ease: 'cubic.out' }) }
      if (f2.value) { f2.value.visible = true; gsap.to(f2.value.position, { y: 18, duration: 0.8, ease: 'cubic.out' }) }
      if (f3.value) { f3.value.visible = true; gsap.to(f3.value.position, { y: 35, duration: 0.8, ease: 'cubic.out' }) }
    }
    camera.animateTo(preset, 0.8)
  }
})

// ======= Init =======
onMounted(async () => {
  try {
    // Load textures in parallel
    const [matcapRoughness, matcapReflection] = await Promise.all([
      loadTexture('/textures/matcap_roughness_3.webp'),
      loadTexture('/textures/matcap_reflection.webp'),
    ])

    const shaderMats = useShaderMaterials({ matcapRoughness, matcapReflection })

    // Load HDR environment map
    const rgbeLoader = new RGBELoader()
    const hdrTexture = await new Promise<THREE.Texture>((resolve, reject) => {
      rgbeLoader.load('/textures/roomhdr_blue.hdr', resolve, undefined, reject)
    })
    hdrTexture.mapping = THREE.EquirectangularReflectionMapping

    // Init light uniforms for each floor
    for (const fc of FLOOR_CONFIGS) {
      lightUniforms.initFloor({
        floorId: fc.id,
        numLights: fc.numLights,
        positions: fc.lights,
        floorY: fc.collapsedY,
      })
    }

    // Load and process GLBs
    const floorRefs = [f1, f2, f3]
    for (let i = 0; i < FLOOR_CONFIGS.length; i++) {
      const fc = FLOOR_CONFIGS[i]
      const scene = await loadGLB(fc.path)
      scene.position.set(0, fc.collapsedY, 0)

      const floorLightUnis = lightUniforms.getFloorUniforms(fc.id)
      applyShaderMaterials(scene, fc, shaderMats, floorLightUnis, hdrTexture)

      // Register device nodes for animation
      registerDeviceNodes(fc.id, scene)

      floorRefs[i].value = scene
    }

    // Setup reactive watchers: worldStore → SDF light uniforms
    setupLightWatchers()
  } catch (e) {
    console.error('Load error:', e)
  }
})

// ======= Pointer events =======
let canvasEl: HTMLCanvasElement | null = null

onMounted(() => {
  setTimeout(() => {
    canvasEl = document.querySelector('canvas')
    if (canvasEl) {
      canvasEl.addEventListener('pointerdown', camera.onPointerDown)
      canvasEl.addEventListener('pointermove', camera.onPointerMove)
      canvasEl.addEventListener('pointerup', camera.onPointerUp)
      canvasEl.addEventListener('pointerleave', camera.onPointerUp)
      canvasEl.addEventListener('wheel', camera.onWheel, { passive: true })
    }
  }, 100)
})

onBeforeUnmount(() => {
  if (canvasEl) {
    canvasEl.removeEventListener('pointerdown', camera.onPointerDown)
    canvasEl.removeEventListener('pointermove', camera.onPointerMove)
    canvasEl.removeEventListener('pointerup', camera.onPointerUp)
    canvasEl.removeEventListener('pointerleave', camera.onPointerUp)
    canvasEl.removeEventListener('wheel', camera.onWheel)
  }
})
</script>

<template>
  <div class="w-full h-full absolute inset-0 scene-container">
    <TresCanvas
      clear-color="#1e242d"
      :antialias="true"
      :tone-mapping="1"
      :tone-mapping-exposure="1"
    >
      <TresPerspectiveCamera
        :position="[-50, 60, 80]"
        :fov="12"
        :near="1"
        :far="300"
      />

      <!-- No directional lights — matcap shader provides all shading -->
      <TresAmbientLight :intensity="0.05" color="#e0dcd4" />

      <!-- Camera + Light update loop -->
      <RenderLoop />

      <!-- Floor models -->
      <primitive v-if="f1" :object="f1" />
      <primitive v-if="f2" :object="f2" />
      <primitive v-if="f3" :object="f3" />

      <!-- Ground with radial gradient -->
      <TresMesh :position="[0, -0.05, 0]" :rotation-x="-Math.PI / 2">
        <TresPlaneGeometry :args="[200, 200]" />
        <primitive :object="groundMaterial" attach="material" />
      </TresMesh>
    </TresCanvas>
  </div>
</template>

<script lang="ts">
// Renderless component inside TresCanvas for per-frame updates
import { defineComponent, toRaw } from 'vue'
import * as THREE from 'three'
import { useLoop, useTres } from '@tresjs/core'
import { useSphericalCamera } from '@/composables/useSphericalCamera'
import { useLightUniforms } from '@/composables/useLightUniforms'
import { updateDeviceAnimations } from '@/composables/useDeviceAnimations'

const RenderLoop = defineComponent({
  name: 'RenderLoop',
  setup() {
    const cam = useSphericalCamera()
    const lights = useLightUniforms()
    const { camera, scene } = useTres()
    const { onBeforeRender } = useLoop()

    // Set scene.background directly (bypasses tone mapping!)
    const bgColor = new THREE.Color(0x1e242d)
    let bgSet = false

    onBeforeRender(({ delta }) => {
      if (!bgSet && scene.value) {
        ;(scene.value as any).background = bgColor
        bgSet = true
      }
      const c = toRaw(camera.value)
      if (c) cam.update(c as any, delta)
      lights.update(delta)
      updateDeviceAnimations(delta)
    })

    return () => null
  },
})

export { RenderLoop }
</script>
