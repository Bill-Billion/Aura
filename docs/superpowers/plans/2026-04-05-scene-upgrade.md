# 写实 3D 场景升级 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将简陋几何体场景替换为模块化写实房屋模型，设备状态实时反映到 3D 场景中。

**Architecture:** 场景 JSON 配置驱动户型布局，SceneManager 加载配置后按房间渲染 RoomModule，每个 RoomModule 用 useGLTF 加载 GLB 模型并在锚点上挂载 DeviceVisual。DeviceVisual 直接读 worldStore 的设备状态，GSAP 动画驱动状态过渡。

**Tech Stack:** Vue 3 + TresJS 5.8 + @tresjs/cientos 5.7 (useGLTF) + Three.js 0.183 + GSAP 3.14 + Sketchfab/Poly Haven CC0 模型

---

## File Structure

| File | Action | Responsibility |
|------|--------|----------------|
| `src/types/scene-config.ts` | Create | 场景配置 TypeScript 类型定义 |
| `public/scenes/apartment_v1.json` | Create | apartment_v1 户型配置 |
| `src/composables/useSceneConfig.ts` | Create | 异步加载场景配置 JSON |
| `src/components/scene/DeviceVisual.vue` | Create | 设备可视化（替代 DeviceMesh.vue） |
| `src/components/scene/RoomModule.vue` | Create | 单个房间：加载 GLB + 渲染设备 |
| `src/components/scene/SceneManager.vue` | Create | 加载配置 → 摆放房间模块 |
| `src/components/scene/SceneRenderer.vue` | Rewrite | 精简为 TresCanvas + SceneManager |
| `src/components/scene/DeviceMesh.vue` | Delete | 被 DeviceVisual.vue 替代 |
| `public/models/rooms/*.glb` | Create | 4 个房间 GLB 模型文件 |

---

### Task 1: 场景配置类型定义

**Files:**
- Create: `src/types/scene-config.ts`

- [ ] **Step 1: 创建类型文件**

```ts
// src/types/scene-config.ts

export type Vec3 = [number, number, number]

export interface DeviceAnchor {
  anchor: Vec3
  type: 'light' | 'hvac' | 'curtain'
}

export interface RoomConfig {
  id: string
  type: string
  model: string
  position: Vec3
  rotation: Vec3
  devices: Record<string, DeviceAnchor>
}

export interface SceneConfig {
  id: string
  name: string
  rooms: RoomConfig[]
}
```

- [ ] **Step 2: TypeScript 编译验证**

Run: `cd /Users/yanghaoran/Code/SmartHomeSim/frontend && npx vue-tsc --noEmit 2>&1 | head -20`
Expected: 无新增类型错误

- [ ] **Step 3: Commit**

```bash
git add src/types/scene-config.ts
git commit -m "feat: add SceneConfig types for modular room system"
```

---

### Task 2: 场景配置 JSON

**Files:**
- Create: `public/scenes/apartment_v1.json`

注意：后端实际设备 ID 为 `light_living_01`、`light_bedroom_01`、`ac_living_01`、`curtain_living_01`。厨房和浴室当前无设备。

- [ ] **Step 1: 创建目录和配置文件**

```bash
mkdir -p public/scenes public/models/rooms
```

```json
{
  "id": "apartment_v1",
  "name": "标准公寓",
  "rooms": [
    {
      "id": "living_room",
      "type": "living_room",
      "model": "models/rooms/living_room.glb",
      "position": [-4, 0, 0],
      "rotation": [0, 0, 0],
      "devices": {
        "light_living_01": {
          "anchor": [0, 2.5, 0],
          "type": "light"
        },
        "ac_living_01": {
          "anchor": [1.5, 2.2, -1],
          "type": "hvac"
        },
        "curtain_living_01": {
          "anchor": [-1, 1.5, -2],
          "type": "curtain"
        }
      }
    },
    {
      "id": "bedroom",
      "type": "bedroom",
      "model": "models/rooms/bedroom.glb",
      "position": [4, 0, 0],
      "rotation": [0, 0, 0],
      "devices": {
        "light_bedroom_01": {
          "anchor": [0, 2.5, 0],
          "type": "light"
        }
      }
    },
    {
      "id": "kitchen",
      "type": "kitchen",
      "model": "models/rooms/kitchen.glb",
      "position": [-4, 0, 5],
      "rotation": [0, 0, 0],
      "devices": {}
    },
    {
      "id": "bathroom",
      "type": "bathroom",
      "model": "models/rooms/bathroom.glb",
      "position": [4, 0, 5],
      "rotation": [0, 0, 0],
      "devices": {}
    }
  ]
}
```

- [ ] **Step 2: Commit**

```bash
git add public/scenes/apartment_v1.json public/models/rooms/.gitkeep
git commit -m "feat: add apartment_v1 scene configuration JSON"
```

---

### Task 3: useSceneConfig composable

**Files:**
- Create: `src/composables/useSceneConfig.ts`

- [ ] **Step 1: 创建 composable**

```ts
// src/composables/useSceneConfig.ts
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
```

- [ ] **Step 2: TypeScript 编译验证**

Run: `cd /Users/yanghaoran/Code/SmartHomeSim/frontend && npx vue-tsc --noEmit 2>&1 | head -20`
Expected: 无新增错误

- [ ] **Step 3: Commit**

```bash
git add src/composables/useSceneConfig.ts
git commit -m "feat: add useSceneConfig composable for loading scene JSON"
```

---

### Task 4: DeviceVisual.vue

**Files:**
- Create: `src/components/scene/DeviceVisual.vue`

这个组件替代 DeviceMesh.vue。接收 deviceId + anchor + deviceType，从 worldStore 读取设备实时状态。

- [ ] **Step 1: 创建 DeviceVisual.vue**

```vue
<script setup lang="ts">
import { computed, watch, ref } from 'vue'
import gsap from 'gsap'
import { useWorldStore } from '@/stores/worldStore'
import type { Vec3 } from '@/types/scene-config'

const props = defineProps<{
  deviceId: string
  anchor: Vec3
  deviceType: 'light' | 'hvac' | 'curtain'
}>()

const worldStore = useWorldStore()
const device = computed(() => worldStore.devices[props.deviceId])

// --- Light ---
const emissiveIntensity = ref(0)
const lightIntensity = ref(0)

const lightColor = computed(() => {
  if (!device.value) return '#ffffff'
  const colorTemp = device.value.state.extra.color_temp ?? 4000
  if (colorTemp < 3500) return '#ff9f43'
  if (colorTemp < 4500) return '#fff5e6'
  return '#dfe6e9'
})

watch(
  () => device.value?.state.extra.brightness,
  (newBrightness: number | undefined) => {
    const brightness = newBrightness ?? 0
    const targetIntensity = device.value?.state.power ? brightness / 100 : 0
    gsap.to(emissiveIntensity, { value: targetIntensity, duration: 0.8, ease: 'power2.out' })
    gsap.to(lightIntensity, { value: targetIntensity * 2, duration: 0.8, ease: 'power2.out' })
  },
  { immediate: true },
)

// --- HVAC ---
const hvacColor = computed(() => {
  if (!device.value?.state.power) return '#9E9E9E'
  const mode = device.value.state.extra.mode
  if (mode === 'cool') return '#4FC3F7'
  if (mode === 'heat') return '#EF5350'
  return '#4FC3F7'
})

// --- Curtain ---
const curtainScaleX = ref(1)
const curtainOpacity = ref(1)

watch(
  () => device.value?.state.extra.open_percent,
  (openPercent: number | undefined) => {
    const open = openPercent ?? 0
    gsap.to(curtainScaleX, { value: 1 - (open / 100) * 0.8, duration: 0.6, ease: 'power2.out' })
    gsap.to(curtainOpacity, { value: 1 - (open / 100) * 0.6, duration: 0.6, ease: 'power2.out' })
  },
  { immediate: true },
)

const curtainColor = computed(() => {
  const open = device.value?.state.extra.open_percent ?? 0
  return open > 50 ? '#4a4a5a' : '#2a2a3a'
})
</script>

<template>
  <template v-if="deviceType === 'light'">
    <TresMesh :position="anchor">
      <TresSphereGeometry :args="[0.12, 16, 16]" />
      <TresMeshStandardMaterial
        :color="lightColor"
        :emissive="lightColor"
        :emissive-intensity="emissiveIntensity"
      />
    </TresMesh>
    <TresPointLight
      :position="[anchor[0], anchor[1] + 0.3, anchor[2]]"
      :intensity="lightIntensity"
      :color="lightColor"
      :distance="8"
    />
  </template>

  <template v-else-if="deviceType === 'hvac'">
    <TresMesh :position="anchor">
      <TresBoxGeometry :args="[0.8, 0.25, 0.18]" />
      <TresMeshStandardMaterial :color="hvacColor" :emissive="hvacColor" :emissive-intensity="device?.state.power ? 0.3 : 0" />
    </TresMesh>
    <TresMesh :position="[anchor[0], anchor[1] - 0.18, anchor[2]]">
      <TresBoxGeometry :args="[0.6, 0.06, 0.04]" />
      <TresMeshStandardMaterial color="#333344" />
    </TresMesh>
  </template>

  <template v-else-if="deviceType === 'curtain'">
    <TresMesh :position="anchor">
      <TresBoxGeometry :args="[1.2 * curtainScaleX, 2.0, 0.04]" />
      <TresMeshStandardMaterial
        :color="curtainColor"
        :opacity="curtainOpacity"
        :transparent="true"
      />
    </TresMesh>
  </template>
</template>
```

- [ ] **Step 2: TypeScript 编译验证**

Run: `cd /Users/yanghaoran/Code/SmartHomeSim/frontend && npx vue-tsc --noEmit 2>&1 | head -20`
Expected: 无新增错误

- [ ] **Step 3: Commit**

```bash
git add src/components/scene/DeviceVisual.vue
git commit -m "feat: add DeviceVisual component with light/hvac/curtain rendering"
```

---

### Task 5: RoomModule.vue

**Files:**
- Create: `src/components/scene/RoomModule.vue`

加载房间 GLB 模型，摆放设备。GLB 加载失败时回退到程序化方块，确保即使没有真实模型也能看到房间结构。

- [ ] **Step 1: 创建 RoomModule.vue**

```vue
<script setup lang="ts">
import { useGLTF } from '@tresjs/cientos'
import type { RoomConfig } from '@/types/scene-config'
import DeviceVisual from './DeviceVisual.vue'

const props = defineProps<{
  room: RoomConfig
}>()

let scene: any = null
let loadError = false

try {
  const { scene: loadedScene } = await useGLTF(props.room.model, { draco: false })
  scene = loadedScene
} catch {
  loadError = true
}

const deviceEntries = Object.entries(props.room.devices)
</script>

<template>
  <!-- Real GLB model -->
  <primitive
    v-if="scene"
    :object="scene"
    :position="room.position"
    :rotation="room.rotation"
  />

  <!-- Fallback: procedural room box -->
  <template v-if="loadError">
    <TresMesh :position="room.position">
      <TresBoxGeometry :args="[4, 3, 4]" />
      <TresMeshStandardMaterial color="#3a3a5e" />
    </TresMesh>
    <!-- Floor -->
    <TresMesh :position="[room.position[0], -0.05, room.position[2]]">
      <TresBoxGeometry :args="[4, 0.1, 4]" />
      <TresMeshStandardMaterial color="#2a2a4e" />
    </TresMesh>
  </template>

  <!-- Devices at anchors (world position = room position + anchor offset) -->
  <DeviceVisual
    v-for="[deviceId, cfg] in deviceEntries"
    :key="deviceId"
    :device-id="deviceId"
    :anchor="[room.position[0] + cfg.anchor[0], room.position[1] + cfg.anchor[1], room.position[2] + cfg.anchor[2]]"
    :device-type="cfg.type"
  />
</template>
```

- [ ] **Step 2: TypeScript 编译验证**

Run: `cd /Users/yanghaoran/Code/SmartHomeSim/frontend && npx vue-tsc --noEmit 2>&1 | head -20`
Expected: 无新增错误

- [ ] **Step 3: Commit**

```bash
git add src/components/scene/RoomModule.vue
git commit -m "feat: add RoomModule with GLB loading and procedural fallback"
```

---

### Task 6: SceneManager.vue

**Files:**
- Create: `src/components/scene/SceneManager.vue`

- [ ] **Step 1: 创建 SceneManager.vue**

```vue
<script setup lang="ts">
import { useSceneConfig } from '@/composables/useSceneConfig'
import RoomModule from './RoomModule.vue'

const { config, loading, error } = useSceneConfig()
</script>

<template>
  <!-- Loading state -->
  <TresMesh v-if="loading" :position="[0, 1.5, 0]">
    <TresBoxGeometry :args="[1, 1, 1]" />
    <TresMeshStandardMaterial color="#666" :emissive="'#333'" :emissive-intensity="0.5" />
  </TresMesh>

  <!-- Error state -->
  <TresMesh v-else-if="error" :position="[0, 1.5, 0]">
    <TresBoxGeometry :args="[2, 0.5, 0.1]" />
    <TresMeshStandardMaterial color="#ff4444" />
  </TresMesh>

  <!-- Loaded: render rooms (each RoomModule has async useGLTF, needs Suspense) -->
  <template v-else-if="config">
    <Suspense>
      <RoomModule
        v-for="room in config.rooms"
        :key="room.id"
        :room="room"
      />
    </Suspense>
  </template>
</template>
```

- [ ] **Step 2: TypeScript 编译验证**

Run: `cd /Users/yanghaoran/Code/SmartHomeSim/frontend && npx vue-tsc --noEmit 2>&1 | head -20`
Expected: 无新增错误

- [ ] **Step 3: Commit**

```bash
git add src/components/scene/SceneManager.vue
git commit -m "feat: add SceneManager for config-driven room layout"
```

---

### Task 7: 重写 SceneRenderer.vue

**Files:**
- Rewrite: `src/components/scene/SceneRenderer.vue`
- Delete: `src/components/scene/DeviceMesh.vue`

将 SceneRenderer 从包含所有硬编码几何体的巨大文件，精简为 TresCanvas + Suspense + SceneManager 的干净入口。

- [ ] **Step 1: 重写 SceneRenderer.vue**

```vue
<script setup lang="ts">
import { TresCanvas } from '@tresjs/core'
import SceneManager from './SceneManager.vue'
</script>

<template>
  <div class="w-full h-full absolute inset-0">
    <TresCanvas clear-color="#0a0a12">
      <TresPerspectiveCamera :position="[12, 12, 12]" :fov="50" :look-at="[0, 0, 2]" />
      <TresAmbientLight :intensity="0.6" />
      <TresDirectionalLight :position="[8, 15, 10]" :intensity="1.2" cast-shadow />
      <TresDirectionalLight :position="[-5, 8, -8]" :intensity="0.3" color="#8888ff" />

      <Suspense>
        <SceneManager />
      </Suspense>
    </TresCanvas>
  </div>
</template>
```

- [ ] **Step 2: 删除旧的 DeviceMesh.vue**

```bash
rm src/components/scene/DeviceMesh.vue
```

- [ ] **Step 3: TypeScript 编译 + 构建验证**

Run: `cd /Users/yanghaoran/Code/SmartHomeSim/frontend && npx vue-tsc --noEmit 2>&1 | head -20`
Expected: 无错误（DeviceMesh 的旧引用已清除）

Run: `cd /Users/yanghaoran/Code/SmartHomeSim/frontend && npm run build 2>&1 | tail -10`
Expected: 构建成功

- [ ] **Step 4: 视觉验证 — 回退模式**

此时没有真实 GLB 模型，SceneManager 会加载配置 → RoomModule 加载 GLB 失败 → 回退到程序化方块 + DeviceVisual 设备。

1. 启动后端：`cd /Users/yanghaoran/Code/SmartHomeSim/backend && python -m uvicorn main:app --host 0.0.0.0 --port 8000`
2. 启动前端：`cd /Users/yanghaoran/Code/SmartHomeSim/frontend && npm run dev`
3. 打开 http://localhost:5173，应看到：
   - 程序化方块代表房间（灰色方块）
   - 设备可视化正常工作（灯光发光、空调指示灯、窗帘透明度）
   - Dashboard 覆盖层正常显示
4. 点击启动仿真，观察设备状态变化是否反映到 3D 场景

- [ ] **Step 5: Commit**

```bash
git add src/components/scene/SceneRenderer.vue
git rm src/components/scene/DeviceMesh.vue
git commit -m "feat: rewrite SceneRenderer with config-driven architecture, remove DeviceMesh"
```

---

### Task 8: 下载并集成写实 GLB 房间模型

**Files:**
- Create: `public/models/rooms/living_room.glb`
- Create: `public/models/rooms/bedroom.glb`
- Create: `public/models/rooms/kitchen.glb`
- Create: `public/models/rooms/bathroom.glb`

这是最关键的视觉任务。需要从 Sketchfab/Poly Haven 找到 4 个风格统一的房间模型。

- [ ] **Step 1: 搜索并下载模型**

在 Sketchfab 上搜索以下关键词，筛选 Free + Downloadable + CC license：

| 房间 | 搜索关键词 | 目标 |
|------|-----------|------|
| 客厅 | `modern living room` (CC, Low-poly) | 含沙发、茶几、电视柜 |
| 卧室 | `modern bedroom` (CC, Low-poly) | 含床、衣柜 |
| 厨房 | `modern kitchen` (CC, Low-poly) | 含橱柜、台面 |
| 浴室 | `bathroom` (CC, Low-poly) | 含淋浴、洗手台 |

筛选标准：
- 风格统一（现代简约/北欧风）
- 面数 < 50k/房间
- GLTF/GLB 格式，含贴图
- 体积优化后 < 5MB/文件

对每个模型：
1. 下载 GLTF/GLB
2. 如需转换格式：`npx gltf-transform copy input.gltf output.glb`
3. 如文件过大，优化：`npx gltf-transform draco input.glb output.glb`
4. 重命名为 `living_room.glb` / `bedroom.glb` / `kitchen.glb` / `bathroom.glb`
5. 放入 `public/models/rooms/`

- [ ] **Step 2: 调整场景配置中的锚点坐标**

加载模型后，在浏览器中检查模型的实际尺寸和坐标原点，调整 `public/scenes/apartment_v1.json` 中的：

- 每个房间的 `position` — 确保房间之间有合理间距
- 每个设备的 `anchor` — 确保灯在天花板、空调在高处墙壁、窗帘在窗户位置
- `rotation` — 如模型朝向不对需旋转

调试方法：在 RoomModule.vue 中临时添加辅助坐标轴：

```vue
<TresAxesHelper :position="room.position" :size="2" />
```

- [ ] **Step 3: 视觉验证 — 真实模型**

1. 确保后端运行中
2. 刷新前端页面
3. 验证：
   - 4 个房间模型加载成功（不再显示回退方块）
   - 房间之间无缝衔接
   - 设备在正确位置渲染
   - 启动仿真后灯光亮度/颜色变化
   - 空调指示灯颜色变化
   - 窗帘开合动画

- [ ] **Step 4: Commit**

```bash
git add public/models/rooms/ public/scenes/apartment_v1.json
git commit -m "feat: add realistic GLB room models and updated scene config"
```

---

### Task 9: 最终打磨和相机调整

**Files:**
- Modify: `src/components/scene/SceneRenderer.vue` (相机参数)
- Modify: `public/scenes/apartment_v1.json` (锚点微调)

- [ ] **Step 1: 调整相机角度**

根据真实模型的整体尺寸，在 SceneRenderer.vue 中调整 PerspectiveCamera 的 position 和 look-at，确保默认视角能看到整个公寓全景。

推荐的视角选项：
- 俯视 45°：`position=[12, 12, 12]` `look-at=[0, 0, 2]`
- 正面偏上：`position=[0, 10, 14]` `look-at=[0, 0, 0]`

选一个能看到 4 个房间全景的视角。

- [ ] **Step 2: 调整环境光照**

根据真实模型的材质效果，微调光照：
- 如果场景太暗 → 提高 AmbientLight intensity 或 DirectionalLight intensity
- 如果场景太亮 → 降低光照
- 确保设备发光效果（PointLight）在真实环境中明显可见

- [ ] **Step 3: 构建验证**

Run: `cd /Users/yanghaoran/Code/SmartHomeSim/frontend && npm run build 2>&1 | tail -10`
Expected: 构建成功，无错误

- [ ] **Step 4: Commit**

```bash
git add src/components/scene/SceneRenderer.vue public/scenes/apartment_v1.json
git commit -m "feat: polish camera angle, lighting, and anchor positions"
```

---

## Spec Coverage Check

| Spec 需求 | 对应 Task |
|-----------|----------|
| 模块化房间 GLB 加载 | Task 5 (RoomModule) + Task 8 (模型下载) |
| 场景配置 JSON 驱动 | Task 1 (类型) + Task 2 (JSON) + Task 3 (composable) |
| 设备状态实时反映 | Task 4 (DeviceVisual) — GSAP + worldStore |
| 灯光亮度/色温动态 | Task 4 — lightColor + emissiveIntensity + PointLight |
| 空调指示灯颜色 | Task 4 — hvacColor computed |
| 窗帘开合动画 | Task 4 — curtainScaleX + curtainOpacity + GSAP |
| 场景配置驱动户型 | Task 6 (SceneManager) + Task 7 (SceneRenderer) |
| GLB 加载失败回退 | Task 5 — loadError fallback |
| 写实模型资源 | Task 8 — 下载 + 优化 + 集成 |
| 相机和光照打磨 | Task 9 |
