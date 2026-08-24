# AuraBench 可复现实验契约

## 冻结输入

每个有效运行必须记录：

- scenario ID、schema version 与 contract hash；
- counterfactual group、variant 与 TraceSpec hash；
- event-relative 干预的 anchor event ID、注入 `seq`/模拟时间和预期前后继；
- seed、初始状态 hash 与命名 RNG 子流约定；
- source、simulator、Agent、evaluator 与 resolver revision；
- 模型/provider/prompt hash/temperature/token cap/timeout；
- Agent topology、governance、observation model 与 repetition；
- 预算和运行模式（rule/mock/recorded/live）。

不得记录 API key、认证 header、raw secret 或未脱敏 provider payload。

## 时间与随机性

- 设备调度、居民反应、观测延迟和 verifier 窗口一律基于模拟时间；
- 禁止用 `sleep()` 或墙钟竞态表达 benchmark 语义；
- 每类噪声/决策使用稳定命名 RNG 子流，新增子流不得移动现有随机序列；
- 相同输入、seed、source revision 和 recorded provider 必须产生 byte-identical canonical trace 与相同终态 hash。

## 工件

run 继续使用 sealed `run.json`、`events.jsonl` 和既有完整性校验。实验目录只引用 run ID 与事件日志摘要，不复制或改写原始 trace。

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
