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

人审工件同时绑定 `pair_set_hash`、resolved matrix hash、results manifest seal、run inventory seal 和 sealed run 的 `source_revision`。运行前状态必须明确为两个未分配 reviewer slot 和 `pending`，不得填写虚构身份或日期。运行完成后，逐 cell inventory 重新验证 result seal、`run.json` hash 和 finalized event-log seal；只有两个不同 reviewer 对同一批证据提交覆盖全部 pair 的不可变工件，最终 freeze gate 才能转为 `approved`。分歧必须保留原工件并进入单独 adjudication。

PR20 的封存顺序固定为：

```bash
python -m backend.experiments inventory-pilot \
  --resolved-matrix <experiment-root>/resolved-matrix.json \
  --result-root <experiment-root> \
  --benchmark-manifest benchmarks/aurabench-dev/manifest.json \
  --results-manifest <analysis-dir>/results-manifest.json \
  --output benchmarks/aurabench-dev/freeze/run-inventory.json

python -m backend.experiments freeze-pilot \
  --bundle-root benchmarks/aurabench-dev \
  --result-root <experiment-root> \
  --benchmark-manifest benchmarks/aurabench-dev/manifest.json \
  --resolved-matrix benchmarks/aurabench-dev/freeze/resolved-matrix.json \
  --results-manifest benchmarks/aurabench-dev/freeze/results-manifest.json \
  --run-inventory benchmarks/aurabench-dev/freeze/run-inventory.json \
  --review benchmarks/aurabench-dev/reviews/<reviewer-1>.json \
  --review benchmarks/aurabench-dev/reviews/<reviewer-2>.json \
  --output benchmarks/aurabench-dev/freeze.json

python -m backend.experiments validate-freeze \
  benchmarks/aurabench-dev/freeze.json \
  --result-root <experiment-root> --require-approved
```

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
- 明确 provider、model/version 与计费模式；按量计费研究必须冻结总预算，并在达到预算 80% 时停止启动新 live cell；token plan 研究不设置虚假的美元停机线，但仍逐 run 记录 token、调用数和按冻结价格估算的成本；
- recording miss、歧义或不完整时 fail closed，禁止静默降级到其他模型。

PR21 的 Option B 子研究固定为 `anthropic_compatible / MiniMax-M3`，并把
`https://api.minimaxi.com/anthropic` 作为唯一允许的 HTTPS endpoint 冻结进研究契约、
预检回执和 paid-run provenance；协议版本、`max_tokens=1200`、实际超时、严格输出模式
及 decision schema SHA-256 同样进入冻结契约，任何环境漂移都会在网络调用前被拒绝。
严格模式还要求 provider 回包中的实际 `model` 字段逐次等于 `MiniMax-M3`，并把实际模型
写入 preflight、成本台账、capture recording 与最终汇总；缺失或上游降级都会 fail closed。
实验条件固定为
`domain_multi + aura + stale_offline`，使用 8 个 dynamic 场景族 × 3 个 seed 形成
24 个实例。每个实例严格执行 `3 live + 1 capture + 3 replay`，合计 168 个槽位；
其中 96 个槽位访问 provider，72 个 replay 槽位由不具备网络能力的 sentinel 守住。
作者清单位于 `benchmarks/aurabench-m3-substudy/manifest.yaml`。
token plan 的 runner 显式使用 `telemetry_only` 成本策略：仍记录每次调用、token 和冻结
价格估算值，但美元阈值在代码路径上不参与放行决策。最终 scientific gate 必须重新验证
sealed preflight，并把 preflight seal 写入 results manifest。

执行顺序固定为：

```bash
python -m backend.experiments resolve-llm-substudy \
  benchmarks/aurabench-m3-substudy/manifest.yaml \
  --output <substudy-root>

python -m backend.experiments preflight-llm-substudy \
  <substudy-root>/resolved-substudy.json \
  --output <substudy-root>

python -m backend.experiments run-llm-substudy \
  <substudy-root>/resolved-substudy.json \
  --output <substudy-root>

python -m backend.experiments summarize-llm-substudy \
  <substudy-root>/resolved-substudy.json \
  --output <substudy-root>
```

预检只发一条结构化请求，并把 provider、model、source revision、token usage 与响应
摘要写入 sealed `preflight.json`；同时记录实际使用的 `tool_use` 或 `text_json` transport，
不保存 API key 或原始认证信息。MiniMax Anthropic 兼容接口只支持 `tool_choice=auto/none`，
因此两种 transport 都是合法观测，schema 校验失败仍直接形成无效证据。没有与 resolved study
完全一致的预检回执，runner 不启动任何 paid slot。每个 slot result 为 create-only；
resume 只跳过重新验证通过的 admitted 证据，失败或无效证据不会原地重试；如需重跑，
必须使用新的 output root，防止“重试直到成功”污染模型可靠性结论。capture 必须有完整
recording manifest；replay 必须零 billable
call、零 miss、无 fallback，并与 capture 在声明的 provenance 差异之外保持 canonical trace、
终态与核心评价结果一致。行为评价等价比较保留 outcome、criteria、终态和所有效果指标，
只排除离线 replay 按设计不会复现的 `first_action_latency_ms` 网络传输耗时；若延迟阈值实际
改变 outcome 或 criteria，门仍会失败。v2 录制的 JSONL 同时保存请求进入序号、每次响应完成
时已有多少请求进入 provider，以及 provider 完成顺序；回放先等待相同请求水位，再按完成顺序
放行，避免零延迟 replay 抢在 capture 网络等待期间的独立事件之前返回。只有 168/168 admitted
且 72/72 replay equivalent 才能生成
`scientific_gate=passed` 的 sealed results manifest。

模型已返回且产生 token usage、但结构化内容为 `invalid_output` 时，PR21 不允许规则策略
代打，也不把它当作可重抽的基础设施故障。研究专用 wrapper 生成零命令 decision，事件流
必须出现 `reasoning.provider_failure_noop(fallback_strategy=none)`；admission 还会验证相同
agent/correlation 下没有设备动作。slot 与最终 manifest 分别封存失败次数和原因。timeout、
HTTP/凭证错误、预算阻断、录制缺失或损坏仍属于无效证据。该 strict 语义只作用于本子研究，
不改变产品运行时默认的安全 fallback。

动态场景中，模型没有产生任何匹配 action anchor 的动作不是基础设施无效运行：runner
保留 `benchmark.perturbation_phase_violation(reason=anchor_not_observed)` 并正常封存，评价器
将其计为 `perturbation_phase_valid` 失败。其他 phase violation（注入失败、时序逆序、运行时
不支持等）仍然使证据无效。这样不会通过排除“不行动”的模型结果来抬高表现。

## 发布门

发布结果必须能从 manifest、聚合脚本和 sealed run 重建；每张图表可追溯到 cell ID 与 run ID。无效运行、未支持假设和超出预算的指标必须与正向结果一起报告。
