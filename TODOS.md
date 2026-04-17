# TODOS

## Engine
- **Priority:** P0 — SimulationEngine tick 循环替换为事件驱动
  - **Why:** 用户行为/环境状态变化不产生 delta，前端温度/ occupancy/位置永远过时
  - **Pros:** 真实智能家居是事件驱动的，可观测性平台需要实时反映状态变化
  - **Cons:** 架构变更范围大，需要重新设计 SimulationEngine 核心循环
  - **Context:** Red Team CRITICAL finding — `simulation.py` 中 `user_sim.step()` 和 `env_sim.step()` 的状态变更不经过 StateManager，前端永远看不到温度/occupancy变化
  - **Depends on:** 无

- **Priority:** P0 — EventBus.publish() 添加异常隔离
  - **Why:** 单个 handler 异常导致所有后续 handler 不执行，影响整个事件链
  - **Pros:** 一个 handler 崩溃不影响其他订阅者
  - **Cons:** 需要设计错误传播策略（静默吞掉 vs 事件失败通知）
  - **Context:** `event_bus.py` — publish() 没有 try/except 包裹单个 handler 调用
  - **Depends on:** 无

- **Priority:** P1 — EventBus._history 重新赋值破坏引用
  - **Why:** `_history = _history[-max_history:]` 创建新列表，外部持有的旧引用失效
  - **Pros:** 修复后所有订阅者都能可靠访问历史
  - **Cons:** 微小性能开销（切片 vs 截断）
  - **Context:** `event_bus.py` — _history 截断逻辑
  - **Depends on:** 无

## Agent System
- **Priority:** P0 — Agent 升级为 LLM 自治系统
  - **Why:** 当前是规则引擎，创始人明确要求 LLM 驱动的意图识别和任务拆分
  - **Pros:** 真实的 Agent 行为可观测，符合产品定位
  - **Cons:** 需要 LLM API 集成，推理延迟，成本
  - **Context:** autoplan User Challenge — 用户选择保留 LLM-driven Agent 作为需求
  - **Depends on:** Engine 事件驱动改造

- **Priority:** P1 — AgentRuntime 单 agent 崩溃隔离
  - **Why:** 一个 agent 异常导致该 tick 所有 agent 停止执行
  - **Pros:** 系统鲁棒性
  - **Cons:** 微小复杂度增加
  - **Context:** `runtime.py` — tick 循环无 per-agent try/except
  - **Depends on:** 无

- **Priority:** P2 — LightingAgent._parse_hour() 畸形时间处理
  - **Why:** 非标准时间字符串导致 agent 崩溃
  - **Pros:** 防御性编程
  - **Cons:** 无
  - **Context:** `lighting.py` — _parse_hour 对非 "HH:MM" 格式崩溃
  - **Depends on:** 无

## State Management
- **Priority:** P1 — StateManager setattr 绕过 Pydantic 验证
  - **Why:** `setattr(device, key, value)` 跳过模型验证，可能写入非法状态
  - **Pros:** 数据完整性保证
  - **Cons:** 需要使用 Pydantic 的 model_validate 或特定 setter
  - **Context:** `state_manager.py` — apply_delta 方法
  - **Depends on:** 无

- **Priority:** P1 — CMD_SIM_RESET 并发竞态
  - **Why:** 并发 reset 创建孤立 StateManager，旧 WS 连接引用旧状态
  - **Pros:** 多客户端场景下状态一致
  - **Cons:** 需要加锁或原子操作
  - **Context:** `main.py` — reset handler 无并发保护
  - **Depends on:** 无

## Physics & Simulation
- **Priority:** P1 — 温度模型过冲 7-12 度/tick
  - **Why:** 每个仿真步长温度变化过大，不符合真实物理
  - **Pros:** 仿真结果更可信
  - **Cons:** 需要调参或重写热力学模型
  - **Context:** `environment.py` — TemperatureSimulator
  - **Depends on:** 无

- **Priority:** P2 — config.toml 未被使用
  - **Why:** 存在配置文件但无代码读取，容易误导
  - **Pros:** 消除死代码，或实现配置驱动
  - **Cons:** 需要决定是删除还是实现
  - **Context:** `backend/config.toml` — 无任何 import/读取
  - **Depends on:** 无

## Frontend
- **Priority:** P0 — 前端测试框架搭建
  - **Why:** 零前端测试，任何改动都可能引入回归
  - **Pros:** 安全重构，信心保证
  - **Cons:** Vitest + @testing-library 配置工作量
  - **Context:** specialist review — Testing specialist 指出前端零覆盖
  - **Depends on:** 无

- **Priority:** P1 — GLB 模型缓存 GPU 内存泄漏
  - **Why:** useGLBLoader 模块级缓存从不清理，长时间运行后 GPU 内存溢出
  - **Pros:** 稳定长时间运行
  - **Cons:** 需要设计缓存失效策略
  - **Context:** `useGLBLoader.ts` — module-level cache never cleared
  - **Depends on:** 无

- **Priority:** P1 — 球形相机抖动
  - **Why:** useSphericalCamera 每帧双重相机更新导致画面抖动
  - **Pros:** 流畅 3D 体验
  - **Cons:** 需要合并更新逻辑
  - **Context:** `useSphericalCamera.ts` — dual camera update per frame
  - **Depends on:** 无

- **Priority:** P2 — worldStore.applySingleDelta 幽灵对象
  - **Why:** 对不存在的设备 ID 创建空对象而非拒绝
  - **Pros:** 数据一致性
  - **Cons:** 需要验证设备存在性
  - **Context:** `worldStore.ts` — applySingleDelta silently creates ghost objects
  - **Depends on:** 无

- **Priority:** P2 — SceneRenderer canvas querySelector 竞态
  - **Why:** DOM 尚未渲染时 querySelector('canvas') 返回 null
  - **Pros:** 避免初始化崩溃
  - **Cons:** 需要使用 ref 或 onMounted
  - **Context:** `SceneRenderer.vue` — querySelector race
  - **Depends on:** 无

## Completed

(无)
