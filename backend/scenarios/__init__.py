"""场景系统：v1/v2 ScenarioSpec、typed TraceSpec 与 YAML 库加载。

对外入口只有这三样，其余模块内部实现不保证稳定：
  - ``backend.scenarios.spec``      —— v1 运行契约（含 §4.1 根事件分类学常量）
  - ``backend.scenarios.spec_v2``   —— AuraBench v2 扩展（仍是 v1 的运行时子类）
  - ``backend.scenarios.trace_spec``—— PR-4 消费的 typed temporal AST
  - ``backend.scenarios.loader``    —— ``load_library(dirs)`` / ``load_scenario_file(path)``
  - ``backend.models.versioning``—— §14 版本比较
"""
