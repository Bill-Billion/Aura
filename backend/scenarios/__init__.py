"""场景系统（spec §5）：ScenarioSpec 契约 + YAML 库加载 + §14 版本兼容。

对外入口只有这三样，其余模块内部实现不保证稳定：
  - ``backend.scenarios.spec``      —— 场景形状的唯一来源（含 §4.1 根事件分类学常量）
  - ``backend.scenarios.loader``    —— ``load_library(dirs)`` / ``load_scenario_file(path)``
  - ``backend.scenarios.versioning``—— §14 版本比较（S4-T1 将整体搬进 backend/models/）
"""
