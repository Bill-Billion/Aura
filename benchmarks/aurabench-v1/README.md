# AuraBench-v1

AuraBench-v1 计划包含 48 对 static/dynamic 场景，共 96 个场景。PR23 只冻结目录、证据和数据切分，`catalog.yaml` 中的 48 对任务全部标记为 `planned`；没有场景 YAML 就不算已实现。

数据切分固定为：

- dev：24 对，每个场景族 3 对；
- validation：8 对，每个场景族 1 对；
- test：16 对，每个场景族 2 对。

每个场景族恰好包含一对负对照。static 与 dynamic 孪生任务必须留在同一 split，同源模板不能跨 split。场景设计的原始论文、官方数据集、适用范围和排除项记录在 [`evidence/scenario-evidence.md`](evidence/scenario-evidence.md)，机器可读来源注册表是 [`sources.json`](sources.json)。

运行目录校验：

```bash
python -m backend.experiments validate-catalog \
  benchmarks/aurabench-v1/catalog.yaml
```

校验器会检查 48 对规模、八个场景族、24/8/16 切分、每族一对负对照、模板跨 split 泄漏、六种干预因素覆盖、来源哈希和证据标签；8 个继承项还会与 `aurabench-dev` 的原始 manifest、场景契约和 pair fingerprint 逐项核对。它不会联网，也不会把 `planned` 任务当作可执行任务。证据标签校验只证明“引用覆盖了所需证据类别”，具体自然语言论断仍由人工审阅。

后续 PR 的状态变化：

1. PR24 实现观测延迟、权限检查和拆分后的结果契约，并加入 static/dynamic 单因素关系与负对照不变性的机器验收门；
2. PR25 至 PR28 每次实现两个场景族，并为每对任务补 static/dynamic 引用；
3. PR29 组装矩阵、运行实验并完成两名独立审阅者的复核；
4. 所有场景都实现且审阅通过后，目录才能进入 `sealed` 状态。
