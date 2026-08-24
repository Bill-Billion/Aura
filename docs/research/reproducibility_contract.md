# AuraBench 可复现实验契约

## 冻结输入

每个有效运行必须记录：

- scenario ID、schema version 与 contract hash；
- counterfactual group、variant 与 TraceSpec hash；
- event-relative 干预的 anchor event ID、注入 `seq`/模拟时间和预期前后继；
- proposal 观察到的世界版本、有限假设集，以及执行前复核失败时的实际值；
- seed、初始状态 hash 与命名 RNG 子流约定；
- source、simulator、Agent、evaluator 与 resolver revision；
- 模型/provider/prompt hash/temperature/token cap/timeout；
- Agent topology、governance、observation model 与 repetition；
- 预算和运行模式（rule/mock/recorded/live）。

不得记录 API key、认证 header、raw secret 或未脱敏 provider payload。

proposal 假设只能来自 Agent 当次推理所见的可观测快照，不能读取 ground truth 或执行时 oracle。假设路径、期望值和失效证据必须进入事件日志，保证同一 recorded run 可以复核为何某份计划被丢弃。PR14 先冻结 Lighting resident-state pilot；其他 Agent 与 LLM 完整 prompt slice 的依赖由 observation-model 契约继续冻结。

## 时间与随机性

- 设备调度、居民反应、观测延迟和 verifier 窗口一律基于模拟时间；
- 禁止用 `sleep()` 或墙钟竞态表达 benchmark 语义；
- 每类噪声/决策使用稳定命名 RNG 子流，新增子流不得移动现有随机序列；
- 相同输入、seed、source revision 和 recorded provider 必须产生 byte-identical canonical trace 与相同终态 hash。

## 工件

run 继续使用 sealed `run.json`、`events.jsonl` 和既有完整性校验。实验目录只引用 run ID 与事件日志摘要，不复制或改写原始 trace。

pilot 根目录还必须提交 `manifest.json`。manifest 冻结 matrix contract hash、seed、每个 static/dynamic 场景 contract hash、pair fingerprint、`pair_set_hash`，以及 human-review 协议和状态工件的内容 hash。验证器重新加载 YAML 和设备注册表计算这些值，并要求 matrix 的 scenario/seed 轴与 manifest 完全一致；任一漂移都 fail closed。

人审工件同时绑定 `pair_set_hash` 和 sealed run 的 `source_revision`。PR16 的初始状态必须明确为两个未分配 reviewer slot 和 `pending`，不得填写虚构身份或日期；验证器在密封 run 绑定于 PR20 落地前拒绝任何人工填写的终态。此后只有两个不同 reviewer 对同一 revision 提交覆盖全部 pair 的不可变工件，gate 才能转为 `approved`；分歧必须保留原工件并进入单独 adjudication。

旧 ScenarioSpec 1.x 与历史工件必须继续可加载和重评；2.x 采用新增字段和新 major，不原地迁移或改写 v1 文件。未知 major 必须 fail closed。

## 恢复与重试

唯一运行键为：

```text
matrix_version / cell_id / scenario_id / seed / repetition / source_revision
```

只有 seal 和全部 fingerprint 校验通过的 completed cell 可被 resume 跳过。科学 FAIL 不自动重试；基础设施 ERROR 仅在显式开启时重试，并记录 attempt。配置 hash 不同的同键结果必须创建新 experiment revision，不能覆盖。

## Live 模型

核心 benchmark 不依赖网络或新凭据。live 模型只用于用户明确授权的固定子集，并要求：

- `AURA_ALLOW_LIVE_LLM=1`；
- 明确 provider、model/version 与总预算；
- 达到预算 80% 时停止启动新 live cell；
- recording miss、歧义或不完整时 fail closed，禁止静默降级到其他模型。

## 发布门

发布结果必须能从 manifest、聚合脚本和 sealed run 重建；每张图表可追溯到 cell ID 与 run ID。无效运行、未支持假设和超出预算的指标必须与正向结果一起报告。
