# AuraBench pilot 双人复核协议

## 目的

两名互不代签的复核者独立检查同一份 pilot 合约，确认每个反事实 pair 的干预、oracle 和 TraceSpec 能支撑科学比较。复核不是代码 review，也不能用模型跑分替代。

## 冻结输入

每份复核工件必须绑定 `manifest.json` 中的 `pair_set_hash` 和同一个 `source_revision`，并覆盖 manifest 列出的全部 pair。只要场景、设备注册表、pair 指纹或评价契约变化，旧复核立即失效。

复核者应读取 static/dynamic YAML、运行时干预语义和一次密封的动态轨迹。动态轨迹必须同时包含 `benchmark.perturbation_injected` 与对应的物理干预事件；只有注入标记不算干预已经实现。

## 四项逐对判断

每项只能记录 `true` 或 `false`，并给出非空理由：

1. `intervention_realized`：声明的单一干预确实由 runtime 实现，并在轨迹中留下物理证据；
2. `oracle_reasonable`：干预后的期望结果与时间窗口符合智慧家庭常识和当前执行语义；
3. `only_declared_difference`：static/dynamic 除身份、干预和干预响应外没有其他任务差异；
4. `tracespec_allows_reasonable_policies`：TraceSpec 排除错误行为，但不会只允许某一条硬编码策略。

## 独立性与提交格式

两个 reviewer ID 必须不同。复核者不得查看另一人的判断后再填写自己的结果。每人提交一个新的、不可原地改写的 JSON 文件，格式如下：

```json
{
  "human_review_schema_version": "1.0",
  "benchmark_id": "aurabench_dev_pilot",
  "pair_set_hash": "<manifest pair_set_hash>",
  "source_revision": "<sealed run source revision>",
  "reviewer_id": "<stable reviewer id>",
  "submitted_at": "<ISO-8601 timestamp>",
  "assessments": [
    {
      "group_id": "<pair group id>",
      "intervention_realized": true,
      "oracle_reasonable": true,
      "only_declared_difference": true,
      "tracespec_allows_reasonable_policies": true,
      "rationale": "<specific evidence and reasoning>"
    }
  ]
}
```

## Gate

- 两份独立复核均覆盖全部 pair 且四项全为 `true`，gate 才能为 `approved`；
- 任一项为 `false`，gate 必须为 `needs_adjudication`；第三名复核者通过单独工件裁决，不得覆盖原始复核；
- 缺人、重复 reviewer、hash/source 不一致或工件不完整时，gate 保持 `pending` 或验证失败；
- PR16 只提交未分配的 pending 状态，绝不伪造姓名、日期或通过结论；正式双人复核必须在 PR20 benchmark freeze 前完成。

PR16 的验证器只接受上述未分配 `pending` 状态。PR20 必须先把 reviewer 判断绑定到密封 run、resolved matrix 和 evaluator provenance，才能启用 `approved`/`needs_adjudication` 状态；在此之前，手工填写“已通过”会 fail closed。
