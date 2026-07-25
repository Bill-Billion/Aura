import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

import {
  SCENE_APPLY_COMMAND,
  SCENE_PRESETS,
  buildSceneApplyPayload,
} from '../src/utils/scenePresets.ts'

const SCENE_SELECTOR_VUE = new URL(
  '../src/components/dashboard/SceneSelector.vue',
  import.meta.url,
)
const SCENE_DEFINITIONS_YAML = new URL(
  '../../backend/config/scene_definitions.yaml',
  import.meta.url,
)

function readSceneSelector(): string {
  return readFileSync(SCENE_SELECTOR_VUE, 'utf-8')
}

/** 后端场景表里的 id（顶层 `scenes:` 下缩进两格的键），不引 yaml 依赖只为读一层键名。 */
function backendSceneIds(): Set<string> {
  const source = readFileSync(SCENE_DEFINITIONS_YAML, 'utf-8')
  const ids = new Set<string>()
  let inScenes = false
  for (const line of source.split('\n')) {
    if (/^scenes:\s*$/.test(line)) {
      inScenes = true
      continue
    }
    if (inScenes && /^\S/.test(line)) break
    const match = inScenes ? /^ {2}([A-Za-z0-9_]+):\s*$/.exec(line) : null
    if (match) ids.add(match[1])
  }
  return ids
}

test('一次场景切换只发一条命令：CMD_SCENE_APPLY + scene_id', () => {
  assert.equal(SCENE_APPLY_COMMAND, 'CMD_SCENE_APPLY')
  assert.deepEqual(buildSceneApplyPayload('away'), { scene_id: 'away' })
})

test('SceneSelector 不再在浏览器里循环发 2×N 条直控', () => {
  const source = readSceneSelector()
  // 这一条是这次改动的全部意义：场景切换必须在后端成为一条可观测的 episode，
  // 浏览器再发 CMD_DEVICE_CONTROL 就又把因果链拆成了 N 条互不相干的直控。
  assert.equal(
    source.includes('CMD_DEVICE_CONTROL'),
    false,
    'SceneSelector 又出现了 CMD_DEVICE_CONTROL：场景语义漏回浏览器了',
  )
  const sendCalls = source.match(/sendCommand\(/g) ?? []
  assert.equal(sendCalls.length, 1, `期望恰好一次 sendCommand，实际 ${sendCalls.length} 次`)
  assert.ok(source.includes('SCENE_APPLY_COMMAND'))
})

test('SceneSelector 里不再出现任何设备取值（场景是后端的数据）', () => {
  const script = readSceneSelector().split('</script>')[0]
  for (const leaked of ['brightness', 'color_temp', 'open_percent', 'target_temp']) {
    assert.equal(script.includes(leaked), false, `SceneSelector 仍在写 ${leaked}`)
  }
})

test('面板列出的每个场景 id 都在后端场景表里', () => {
  const known = backendSceneIds()
  assert.ok(known.size > 0, '没能从 scene_definitions.yaml 读到场景 id')
  for (const preset of SCENE_PRESETS) {
    assert.ok(
      known.has(preset.id),
      `场景 '${preset.id}' 后端不认识，点下去只会得到一条 unknown_scene ERROR`,
    )
  }
})

test('面板保留原来的四个预设（demo 行为不能退化）', () => {
  assert.deepEqual(
    SCENE_PRESETS.map((preset) => preset.id),
    ['reading', 'entertainment', 'away', 'sleep'],
  )
})
