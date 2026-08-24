# AuraBench 分类学

AuraBench 使用反事实配对组织任务。每个动态 episode 必须有一个共享初态、目标、基础 timeline、TraceSpec 和 seed 的静态孪生任务；两者只允许在声明的 perturbation 上不同。

## 八个场景族

1. 状态感知与执行前复核；
2. 隐式意图与合理 no-op；
3. 时间、设备依赖与长周期操作；
4. 多居民偏好和权限冲突；
5. 安防、隐私、舒适与能源跨域冲突；
6. 用户意图、位置和活动的非平稳变化；
7. 部分可观测、延迟与陈旧传感器；
8. 设备、反馈和 Agent 故障恢复。

## 动态干预因子

ScenarioSpec 2.0 首版冻结六类单因素干预：

- `resident_state_change`：规划或执行期间居民位置/活动变化；
- `device_failure`：设备在验证、执行或反馈阶段离线；
- `conflicting_request`：另一居民提出不兼容请求；
- `safety_interrupt`：安全事件打断舒适/能源计划；
- `observation_delay`：Agent 看到延迟状态；
- `feedback_loss`：物理效果发生但反馈丢失。

组合扰动不属于反事实主对照，只能进入单独的 stress split。

## 数据切分

- `dev`：开发与调试，可重复查看结果；
- `validation`：协议和阈值的一次性校准；
- `test`：协议冻结后运行，不得根据结果修改成功条件。

派生自同一 template 的任务不得跨 split。静态与动态孪生任务必须处于同一 split。

## 难度

- `easy`：单居民、单域、单动作、无隐含依赖；
- `medium`：多动作或单一动态干预，需要一次复核/恢复；
- `hard`：多居民或跨域治理，并包含有限时间窗口或故障恢复要求。

难度只描述任务结构，不得根据某个模型的实际成功率事后调整。

## 首版规模门

先运行 8–12 组反事实 pair × 3 seeds 的 pilot。只有科学效应和运行健康通过 gate 后，才扩张到 8 个场景族 × 6 个语义模板 × 2 个 variant × 5 seeds，并另加组合扰动，形成约 600 episodes。
