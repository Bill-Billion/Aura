# Gamemcu 设备注册与可操作化计划

Author: Bill Billion  
Date: 2026-04-18

## 1. 范围和边界

这份计划只覆盖已经在 gamemcu 页面和发布产物里明确确认过的设备类别。

第一批纳入系统并完成接入的类型有五类：灯光、空调、窗帘、风扇、摄像头。环境传感器一并注册，但按只读设备处理。`air` 类型在 gamemcu 的前端代码里只有预留入口，控制器是空实现，这一轮不把它当成已知能力来做。

目标不是只把设备名称塞进状态树，而是让这些设备在我们的系统里具备四件事：能被统一注册、能在世界状态里出现、能在 3D 和右侧壳层里被找到、能按各自能力被控制或查看。

## 2. 当前项目的真实缺口

当前代码已经有一套最小可运行链路，但它还是四设备 demo，不是设备系统。

后端世界状态只支持 `light`、`hvac`、`curtain`、`sensor` 四种类型。默认世界里只有 `light_living_01`、`light_bedroom_01`、`ac_living_01`、`curtain_living_01` 四个设备。`backend/main.py` 直接手写了这些初始设备，导致新增设备必须改主入口。

前端也还是硬编码状态。`frontend/src/components/dashboard/ContextualDevicePanel.vue` 只会渲染灯光、空调、窗帘三种控制面板。`frontend/src/utils/deviceFloorMap.ts` 和 `frontend/src/components/dashboard/HomePanelGroup.vue` 只认识四个设备 ID。`frontend/src/components/scene/SceneRenderer.vue` 现在只把 GLB 节点里的空调和窗帘解析成业务设备，灯光、风扇、摄像头、传感器没有进入统一选中链路。

这意味着现在的系统问题不是“控件不够多”，而是缺少一份统一的设备注册表。后端、前端、3D 场景、右侧面板都在各自写一套设备认知。

## 3. 设计原则

### 3.1 用注册表做单一事实来源

新增一份设备注册表，作为整个项目的单一事实来源。后端默认世界状态、前端楼层归属、右侧显示名称、3D 节点绑定、可控制能力，都从这份注册表派生，不再允许散落在多个组件里各写一套 map。

### 3.2 把“设备存在”与“设备状态”拆开

设备注册信息解决“系统里有哪些设备、在哪一层、叫什么、支持什么能力”。世界状态解决“它现在开着没有、温度多少、风速多少、是否在线”。这两个层次必须分开。否则以后只要增加一个设备，就会反复改初始化代码、楼层映射和面板逻辑。

### 3.3 可控、可查看、只读三类能力要分清

这轮接入不是所有设备都强行套同一种控制模式。

灯光、空调、窗帘、风扇属于可控设备。摄像头属于可查看设备，重点是选择和查看画面状态。传感器属于只读设备，重点是读取温湿度或光照值。设备注册表里必须把这层能力写清楚，前端面板才能按能力渲染，而不是只按 `type` 写死分支。

## 4. 目标设备模型

建议新增一个共享的注册结构 `DeviceRegistryEntry`，至少包含以下字段：

- `id`: 设备稳定 ID，供状态树、WebSocket 和 3D 绑定共用
- `type`: 设备类别，第一批支持 `light | hvac | curtain | fan | camera | sensor`
- `displayName`: 用户界面显示名
- `roomId`: 所属房间
- `floorId`: 所属楼层
- `capabilities`: 设备能力清单
- `defaultState`: 初始运行态，映射到当前 `DeviceStateValues`
- `sceneBindings`: 和 GLB 节点或场景标签的绑定信息
- `uiGroup`: 对应 gamemcu 的归组，取值为 `lighting | device | security | environment`

后端运行时 `DeviceState` 继续保留 `state.power + state.extra` 这套结构，避免一次性推翻现有消息协议。但 `type` 需要扩成六种，前端类型定义也要同步扩展。

## 5. 第一批设备清单

### 5.1 可控设备

灯光：按房间拆成多个灯具，至少覆盖一层客厅、二层卧室，以及后面要补齐的不同楼层和房间灯光。能力包含开关、亮度、色温。

空调：按 gamemcu 的双空调思路至少接入两台。能力包含开关、模式、目标温度、风速。当前系统已经支持目标温度和模式，但还缺风速字段和 UI。

窗帘：至少接入两组窗帘。能力包含开关和开合百分比。

风扇：新增一类设备。能力包含开关、风速、摇头、定时。当前后端没有这类设备，前端也没有控制器，需要完整补齐。

### 5.2 可查看设备

摄像头：至少接入两路。能力不是开关控制，而是在线状态、当前画面源、楼层归属和右侧预览切换。前端需要有 CameraPanel，后端需要提供设备在线状态和模拟预览元数据。

### 5.3 只读设备

环境传感器：至少覆盖温度传感器，后面可扩展湿度和光照。它们要出现在设备注册表和右侧环境分组里，但默认不接受 `CMD_DEVICE_CONTROL`。

## 6. 实施方案

### Phase A：建立统一设备注册表

新增一份设备目录模块，作为 apartment_v1 的默认设备清单。它要替代 `backend/main.py` 里直接手写 `world.devices` 的做法。

这里建议新增：

- `backend/config/device_registry.py`：后端权威设备注册表
- `frontend/src/config/deviceRegistry.ts`：前端镜像注册表，字段和后端保持同构
- `frontend/src/utils/deviceCatalog.ts`：从注册表派生楼层、显示名、分组和筛选结果

如果不想维护两份手写注册表，就把后端注册表通过一个轻量 REST 接口或 `STATE_FULL` 的静态元数据下发给前端。我的建议是第二种更稳，长期只保留后端一份权威注册表。

### Phase B：扩展后端设备类型和命令校验

`backend/engine/state.py` 的 `DeviceState.type` 要扩成六种设备类型。`backend/main.py` 的默认状态初始化改成从注册表生成，不再手填。

WebSocket 指令接收层要从“只认 turn_on/turn_off/set_state”升级成“按能力校验参数”。这一层不需要把协议推翻，可以继续沿用 `CMD_DEVICE_CONTROL`，但要补一层按设备类型的参数白名单：

- light: `brightness`、`color_temp`
- hvac: `target_temp`、`mode`、`speed`
- curtain: `open_percent`
- fan: `speed`、`shake`、`timeout`
- camera: 不走通用 set_state，改成前端本地选择预览源或后端只接受查看类命令
- sensor: 禁止写入，返回明确错误

同时把这些控制动作继续接到现有 `SimEvent` 链路里。这样设备注册补齐后，事件流不会再返工一遍。

### Phase C：补齐默认世界状态和仿真字段

注册表落地以后，默认世界里要真正出现这些设备，而不是只在前端写卡片。

灯光、空调、窗帘继续沿用当前环境模拟逻辑。风扇和摄像头先不进入物理环境计算，但必须进入世界状态。

建议的 `extra` 字段最小集合：

- light: `brightness`、`color_temp`
- hvac: `target_temp`、`mode`、`speed`
- curtain: `open_percent`
- fan: `speed`、`shake`、`timeout`
- camera: `online`、`feed_key`、`preview_label`
- sensor: `sensor_type`、`value`、`unit`

这一层做完后，`STATE_FULL` 就会带出完整设备集合，前端不需要自己猜设备存在与否。

### Phase D：重做前端设备发现和楼层归属

`frontend/src/utils/deviceFloorMap.ts`、`frontend/src/components/dashboard/HomePanelGroup.vue`、`frontend/src/components/dashboard/ContextualDevicePanel.vue` 里的硬编码设备 ID 和房间名都要删掉，统一从注册表读。

右侧壳层需要按 gamemcu 的分组方式显示：照明、设备、安防、环境。这样设备数扩起来以后，右侧不会因为设备数量增长再次变成一长列硬编码按钮。

Contextual panel 改成 capability-driven。也就是先看设备能力，再决定渲染 LightPanel、HVACPanel、CurtainPanel、FanPanel、CameraPanel 还是 SensorPanel，不再只靠 `device.type === 'xxx'` 三分支。

### Phase E：补齐 3D 场景绑定

现在 `SceneRenderer` 只把部分 GLB 节点映射到业务设备，这是第二个大缺口。需要新增一层场景绑定配置，把 GLB 节点名和设备 ID 建立稳定关系。

建议新增 `frontend/src/config/sceneDeviceBindings.ts`，每层维护一份绑定：

- 设备 ID 对应哪些 GLB node 名
- 节点的可点选优先级
- 信息牌锚点
- 摄像头预览锚点
- 传感器标签锚点

`SceneRenderer.vue` 不再写死 `ac` 和 `curtain` 的解析逻辑，而是统一按绑定表注册 `selectableMeshes`。这样灯光、风扇、摄像头、传感器都能进同一套选中链路。

### Phase F：新增设备面板与交互

前端新增三个面板：

- `FanControlPanel.vue`
- `CameraPanel.vue`
- `SensorPanel.vue`

风扇面板负责开关、风速、摇头、定时。摄像头面板负责设备切换、在线状态和预览。传感器面板负责只读显示，不提供写入按钮。

`HomePanelGroup.vue` 里的场景模式也要从“只广播灯光、窗帘、空调”扩成按能力批量下发。离家模式可以关闭风扇，安防模式可以把摄像头卡片前置，但不应该尝试去给传感器发控制命令。

## 7. 建议的文件改动范围

后端主要会改这些位置：

- `backend/engine/state.py`
- `backend/main.py`
- `backend/engine/state_manager.py`
- `backend/engine/simulation.py`
- `backend/config/device_registry.py`（新增）
- `tests/test_main.py`
- `tests/test_state.py`
- `tests/test_state_manager.py`
- `tests/test_simulation.py`

前端主要会改这些位置：

- `frontend/src/types/world-state.ts`
- `frontend/src/composables/useWebSocket.ts`
- `frontend/src/components/dashboard/ContextualDevicePanel.vue`
- `frontend/src/components/dashboard/HomePanelGroup.vue`
- `frontend/src/utils/deviceFloorMap.ts` 或替换为注册表派生逻辑
- `frontend/src/config/deviceRegistry.ts` 或新增的设备目录消费层
- `frontend/src/config/sceneDeviceBindings.ts`（新增）
- `frontend/src/components/dashboard/panels/FanControlPanel.vue`（新增）
- `frontend/src/components/dashboard/panels/CameraPanel.vue`（新增）
- `frontend/src/components/dashboard/panels/SensorPanel.vue`（新增）
- `frontend/src/components/scene/SceneRenderer.vue`

## 8. 交付顺序

建议按这个顺序推进，不要一上来就先补面板。

第一步先做注册表和默认状态生成。第二步做后端命令校验和新的设备类型。第三步做前端设备发现逻辑，把所有硬编码设备映射清掉。第四步补风扇、摄像头、传感器面板。第五步做 3D 场景节点绑定和点选链路。第六步补模式联动和事件流验证。

这个顺序的好处是，每一层都能单独验收。哪怕 3D 绑定还没做完，右侧壳层也已经能显示全部已注册设备。

## 9. 验收标准

这轮完成后，系统至少要满足下面这些结果。

后端 `STATE_FULL` 能返回完整设备清单，不再只有 4 个 demo 设备。右侧壳层能按照明、设备、安防、环境四组看到所有已注册设备。灯光、空调、窗帘、风扇都可以发出有效控制命令并更新状态。摄像头可以被选择并显示在线状态和预览内容。传感器可以展示实时数值，但不会错误地暴露控制按钮。

另外，`SceneRenderer` 里对应设备能被点中，点中之后右侧打开正确面板。`SIM_EVENT` 里能看到新增设备类型触发的 `user.command` 和 `feedback.state_delta`。已有 70 个后端测试不能回归，新增设备类型后还要补命令校验和状态序列化测试。

## 10. 完成这一轮之后的下一步

设备注册补齐以后，下一步不该回到“继续堆 UI 卡片”，而是回到 GSTACK 主线，把这些新增设备正式纳入事件驱动可观测性。

到那时，事件面板里不仅要看到灯光和空调，也要能看到风扇、摄像头、传感器相关的链路。这样后面的 Agent、reasoning 和 observability 才不会再次只围绕四个 demo 设备展开。
