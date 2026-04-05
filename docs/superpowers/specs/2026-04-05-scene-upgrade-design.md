# 写实 3D 场景升级设计

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将当前简陋的几何体 3D 场景替换为写实的模块化房屋模型，并让设备状态实时反映到场景中。

**Architecture:** 场景配置 JSON 驱动户型布局，每个房间加载独立的 GLB 模型，设备可视化组件挂载到锚点上并响应 worldStore 状态变化。

**Tech Stack:** Vue 3 + TresJS + @tresjs/cientos (useGLTF) + Three.js + GSAP + Sketchfab/Poly Haven CC0 模型

---

## 1. 整体架构

场景系统分为 4 层组件：

```
SceneRenderer.vue          入口，挂载 TresCanvas，提供光照和环境
  └─ SceneManager.vue      加载场景配置 JSON，按房间摆放 RoomModule
       └─ RoomModule.vue   每个房间：加载 GLB + 渲染设备可视化
            └─ DeviceVisual.vue  设备动态效果（灯光/空调/窗帘）
```

**关键设计决策：**

1. **场景配置驱动** — 一个 JSON 文件定义户型布局（房间类型、位置、包含的设备及其锚点坐标），不同场景 ID 对应不同 JSON
2. **每个房间 = 一个 GLB 文件** — 从 Sketchfab/Poly Haven 下载，统一风格，通过 `useGLTF` 加载
3. **设备锚点** — JSON 中定义每个设备在房间坐标系中的精确位置，设备可视化组件挂载到锚点上
4. **状态驱动渲染** — `DeviceVisual.vue` 监听 worldStore 中的设备状态，实时更新视觉效果

## 2. 场景配置格式

配置文件路径：`public/scenes/{scene_id}.json`

后端 `scene_id` 为 `apartment_v1`，对应文件 `public/scenes/apartment_v1.json`。

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
        "light_living": {
          "anchor": [0, 2.5, 0],
          "type": "light"
        },
        "ac_living": {
          "anchor": [0, 2.2, 1],
          "type": "hvac"
        },
        "curtain_living": {
          "anchor": [0, 1.5, -2],
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
        "light_bedroom": {
          "anchor": [0, 2.5, 0],
          "type": "light"
        },
        "ac_bedroom": {
          "anchor": [0, 2.2, 1],
          "type": "hvac"
        },
        "curtain_bedroom": {
          "anchor": [0, 1.5, -2],
          "type": "curtain"
        }
      }
    },
    {
      "id": "kitchen",
      "type": "kitchen",
      "model": "models/rooms/kitchen.glb",
      "position": [-4, 0, 4],
      "rotation": [0, 0, 0],
      "devices": {
        "light_kitchen": {
          "anchor": [0, 2.5, 0],
          "type": "light"
        }
      }
    },
    {
      "id": "bathroom",
      "type": "bathroom",
      "model": "models/rooms/bathroom.glb",
      "position": [4, 0, 4],
      "rotation": [0, 0, 0],
      "devices": {
        "light_bathroom": {
          "anchor": [0, 2.5, 0],
          "type": "light"
        }
      }
    }
  ]
}
```

**锚点坐标系：** 锚点坐标相对于房间模型原点。模型加载后锚点跟随房间 `position` 做世界坐标变换。

## 3. 模型资源

### 来源

| 平台 | 许可 | 特点 |
|------|------|------|
| Sketchfab | CC BY / CC0 | 最大的模型库，筛选免费+下载 GLTF |
| Poly Haven | CC0 | 高质量 PBR 资源，文件干净 |
| pmndrs Market | CC0 | 专为 Three.js 优化过的模型 |

### 筛选标准

- 风格：现代/北欧简约，统一色调
- 面数：单房间 < 50k 三角面
- 格式：GLTF/GLB，含 PBR 贴图
- 体积：优化后每房间 2-5MB

### 优化流程

使用 `gltf-transform` CLI 压缩：

```bash
gltf-transform optimize input.glb output.glb \
  --compress draco \
  --texture-compress webp \
  --simplify true
```

### 存放路径

```
public/
  models/
    rooms/
      living_room.glb
      bedroom.glb
      kitchen.glb
      bathroom.glb
  scenes/
    apartment_v1.json
```

## 4. 组件职责

### SceneRenderer.vue（改写）

- 挂载 `TresCanvas`
- 提供全局光照：环境光 + 方向光 + 补光
- 设置 `clear-color` 背景色
- 渲染 `SceneManager` 组件（包裹在 `<Suspense>` 中处理异步模型加载）

### SceneManager.vue（新建）

- fetch 加载场景配置 JSON（根据 worldStore 中的 `scene_id`）
- 为每个房间配置项渲染 `RoomModule`
- 处理加载状态（进度条/骨架屏）

### RoomModule.vue（新建）

- 接收 room 配置（model 路径、position、rotation、devices）
- `useGLTF` 加载房间 GLB 模型
- 渲染 `<primitive :object="scene">` 并应用 position/rotation
- 遍历 devices 配置，为每个设备渲染 `DeviceVisual`，传入设备 ID 和锚点坐标

### DeviceVisual.vue（改写）

接收 props：`deviceId: string`、`anchor: [x, y, z]`、`deviceType: string`

从 worldStore 读取对应设备的实时状态，根据类型渲染：

**灯光（light）：**
- 亮度 → PointLight intensity（0-100% 映射到 0-3.0）
- 色温 → 灯光颜色（2700K 暖黄 #FFB347 ↔ 6500K 冷白 #F5F5DC）
- 发光球体 mesh 的 emissive 强度跟随亮度
- GSAP 平滑过渡所有状态变化

**空调（hvac）：**
- 运行状态 → 指示灯颜色（制冷 #4FC3F7 / 制热 #EF5350 / 关机 #9E9E9E）
- 目标温度 → 可选的浮动标签

**窗帘（curtain）：**
- 开合度 → 窗帘 mesh scale.x 动画（0%=关闭, 100%=打开）
- 材质透明度联动
- GSAP 平滑过渡

## 5. 加载流程

```
App 启动
  → useWebSocket 连接后端
  → 收到 STATE_FULL（包含 scene_id）
  → worldStore.sceneId 更新
  → SceneManager watch(sceneId) 触发
  → fetch /scenes/{sceneId}.json
  → useGLTF 并行加载所有房间 GLB
  → Suspense 显示加载进度
  → 模型就绪 → 渲染房间 + 设备
  → 后续 STATE_DELTA 更新 → DeviceVisual 响应式更新
```

## 6. 文件变更清单

| 操作 | 文件 |
|------|------|
| 改写 | `src/components/scene/SceneRenderer.vue` |
| 新建 | `src/components/scene/SceneManager.vue` |
| 新建 | `src/components/scene/RoomModule.vue` |
| 改写 | `src/components/scene/DeviceMesh.vue` → 重命名为 `DeviceVisual.vue` |
| 新建 | `public/scenes/apartment_v1.json` |
| 新增 | `public/models/rooms/*.glb`（从 Sketchfab 下载） |
| 可能更新 | `src/types/world-state.ts`（如需扩展场景类型定义） |

## 7. 风险与应对

| 风险 | 应对 |
|------|------|
| 找不到风格统一的免费房间模型 | 备选：用 Poly Haven 的 HDRI + 程序化房屋结构 + 单独家具 GLB |
| GLB 文件过大影响加载 | Draco 压缩 + 纹理 WebP 转换 + 懒加载非可见房间 |
| 模型内坐标系与锚点不匹配 | 提供可视化调试工具，在浏览器中实时调整锚点坐标 |
| TresJS useGLTF 与 Suspense 兼容性 | 已验证 @tresjs/cientos v5.7.0 的 useGLTF 支持 Suspense |
