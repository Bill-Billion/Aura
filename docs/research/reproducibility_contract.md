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

## 观察模型契约

`current_projector_v1` 是观察模型族，当前只实现两个确定性条件：

- `perfect`：Agent 获得当前世界和根事件的独立深拷贝；
- `stale_offline`：在线设备报告当前值，离线设备回放最后一次在线值；从未在线过的设备标为 unavailable，不得复制当前物理值。

world snapshot 与 root event 必须由同一次投影产生。每个有效 episode 在任何 planner/controller 消费之前发布 `observation.frame_captured`，封存 condition、model family、contract version、model hash、模拟时间、可观测快照、投影后的根事件及 stale/unavailable 设备集合。`reasoning.perception_snapshot` 和 coordination evidence 只能引用同一 frame hash；resume 会重算并交叉校验这些证据，仅修改 cell/run metadata 不能冒充另一种观察条件。

新 resolved matrix 使用 schema 1.2，并显式冻结 `expected_observation_conditions`。历史 1.1 stale-only 矩阵只允许读取和离线复核，不能继续执行。观察比较固定 scenario、seed、model、repetition、runtime profile、topology、governance 和 source revision，方向统一为 `stale_offline - perfect`；缺少任一条件或固定 provenance 漂移时整组 invalid。

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

统计分析是独立阶段，不在 `run` 或 `summarize` 中隐式触发。原始模式先使用与全局汇总相同的 seal、run provenance 和 fairness gate，把有效 cell 投影为自包含的 `results-manifest.json`；离线模式只能读取这份密封清单，不能重新访问 run 目录。两个模式分别为：

```bash
python -m backend.experiments analyze \
  --resolved-matrix <resolved-matrix.json> \
  --result-root <experiment-root> \
  --benchmark-manifest benchmarks/aurabench-dev/manifest.json \
  --output <analysis-dir>

python -m backend.experiments analyze \
  --results-manifest <analysis-dir>/results-manifest.json \
  --output <rebuild-dir>
```

同一 results manifest 必须字节级重建 `pair-level-results.jsonl`、`aggregate-results.json`、`bootstrap-samples.json`、`error-taxonomy.json`、主表、消融表和 figure data。`artifact-manifest.json` 最后创建并记录每个文件的 hash 和字节数；已有文件只接受 byte-identical 内容，禁止覆盖。human review 为 `pending` 时必须原样写入结果清单，不得伪造 approval。

二元配对使用 exact McNemar 和 paired bootstrap 风险差，连续非正态配对使用 Wilcoxon signed-rank 和 bootstrap 配对差中位数，比例使用 Wilson 区间，多 baseline family 使用完整预注册的 Holm–Bonferroni。所有结果同时报告 effect、95% CI、`n` 和 invalid；缺失任一 arm 时整对不进入配对检验。Final-State Blind Spot 的两个 Wilson 分母分别是各 arm 中 `final_state_success=true` 的运行，不能因另一 arm 不满足终态而删除。

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
