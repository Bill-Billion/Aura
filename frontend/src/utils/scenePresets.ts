/**
 * 场景预设的前门 —— 一次点击 = 一条 WS 命令。
 *
 * 被推翻的现状：SceneSelector.vue 过去在浏览器里按 switch 循环发 2×N 条
 * `CMD_DEVICE_CONTROL`。后端只看到 N 条互不相干的直控，拼不出"这是一次场景切换"这条
 * 因果链——而看得见推理链路正是这个平台的产品；场景语义同时被锁在 .vue 里，headless
 * 脚本与评估器复用不了，命令还绕过了编排与仲裁。
 *
 * 现在场景**是数据**，住在 `backend/config/scene_definitions.yaml`；前端只负责
 * "点了哪个 id"。这里保留的 label/desc 是纯展示文案，任何设备取值都不再出现在前端——
 * 一旦出现，就说明场景语义又漏回浏览器了（frontend/tests/scenePresets.test.ts 在盯）。
 */

/** 一条消息 = 一次场景切换（后端 `backend/agents/scene.py::SCENE_APPLY_MESSAGE_TYPE`）。 */
export const SCENE_APPLY_COMMAND = 'CMD_SCENE_APPLY'

export interface ScenePreset {
  /** 必须是后端场景表里的 id（否则后端回 `unknown_scene` ERROR）。 */
  id: string
  label: string
  desc: string
}

export interface SceneApplyPayload {
  scene_id: string
}

/**
 * 面板上列出的预设。id 取自后端场景表；后端表里还有 home / wake / cooking 三个
 * 供场景脚本使用的场景，不进这个面板（它们不是给人点的）。
 */
export const SCENE_PRESETS: readonly ScenePreset[] = [
  { id: 'reading', label: '阅读模式', desc: '局部暖光和半开窗帘' },
  { id: 'entertainment', label: '娱乐模式', desc: '保持低亮度，强化客厅氛围' },
  { id: 'away', label: '离家模式', desc: '收束灯光和窗帘，压低存在感' },
  { id: 'sleep', label: '睡眠模式', desc: '关闭主要灯光，空调归夜间值' },
]

/** `CMD_SCENE_APPLY` 的载荷（后端 `SceneApplyPayload`：scene_id 必填非空）。 */
export function buildSceneApplyPayload(sceneId: string): SceneApplyPayload {
  return { scene_id: sceneId }
}
