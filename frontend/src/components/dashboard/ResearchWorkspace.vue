<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { storeToRefs } from 'pinia'
import ObservabilityPanel from './ObservabilityPanel.vue'
import RunComparison from './RunComparison.vue'
import ResearchRunCard from './research/ResearchRunCard.vue'
import { useResearchWorkspace } from '@/composables/useResearchWorkspace'
import { useUIStore } from '@/stores/uiStore'
import type { BaselinePolicy, RunSide, RunSlot, WorkspaceView } from '@/types/research-run'
import {
  buildComparisonBundle,
  buildComparisonFilename,
  buildTraceFilename,
  launchMatchesRun,
  recordingSourceCandidates,
} from '@/utils/runComparison'
import { downloadBlobFile, downloadTextFile } from '@/utils/download'
import { toStructuredApiError } from '@/composables/researchApi'
import { focusBeforeInitialize } from '@/utils/workspaceLifecycle'

const props = defineProps<{
  open: boolean
}>()

const emit = defineEmits<{
  close: []
}>()

const uiStore = useUIStore()
const { activeDevice } = storeToRefs(uiStore)
const panelRef = ref<HTMLElement | null>(null)
const workspaceBodyRef = ref<HTMLElement | null>(null)
const {
  view,
  form,
  announcement,
  scenariosResource,
  runsResource,
  scenarios,
  recentRuns,
  selectedScenario,
  slotA,
  slotB,
  activeSide,
  isBusy,
  comparisonInvariant,
  initialize,
  retryScenarios,
  refreshRecentRuns,
  getRawTrace,
  selectScenario,
  copyAParameters,
  startSide,
  retrySide,
} = useResearchWorkspace()

const policyOptions: Array<{ value: BaselinePolicy; label: string; description: string }> = [
  { value: 'rule_based', label: '规则基线', description: '不调用 LLM；确定性规则链。' },
  { value: 'llm_mocked', label: 'LLM Mock', description: '固定 fixture；零网络、可重复。' },
  { value: 'llm_recorded', label: 'LLM 录制 / 回放', description: '有 source 时零网络回放；留空则首次在线录制。' },
  { value: 'llm_live', label: 'LLM Live', description: '使用服务端已配置 provider；可能产生费用。' },
]

const tabs: Array<{ id: WorkspaceView; step: string; label: string }> = [
  { id: 'setup', step: '01', label: 'Setup' },
  { id: 'live', step: '02', label: 'Live' },
  { id: 'compare', step: '03', label: 'Compare' },
]

const bMatchesA = computed(() => {
  if (!slotA.run) return false
  return launchMatchesRun({ scenario_id: form.scenarioId, seed: Number(form.seed) }, slotA.run)
})

const recordingSourceRuns = computed(() => (
  recordingSourceCandidates(recentRuns.value, form.scenarioId, Number(form.seed))
))

const canLaunchA = computed(() => Boolean(form.scenarioId) && !isBusy.value)
const canLaunchB = computed(() => (
  slotA.phase === 'success' && Boolean(form.scenarioId) && bMatchesA.value && !isBusy.value
))
const canCompare = computed(() => (
  slotA.phase === 'success' && slotB.phase === 'success' && comparisonInvariant.value.ok
))

watch(() => props.open, async (isOpen) => {
  if (!isOpen) return
  await nextTick()
  await focusBeforeInitialize(() => panelRef.value?.focus(), initialize)
}, { immediate: true })

watch(view, async () => {
  await nextTick()
  if (workspaceBodyRef.value) workspaceBodyRef.value.scrollTop = 0
})

function requestClose(): void {
  emit('close')
}

function onGlobalKeydown(event: KeyboardEvent): void {
  if (!props.open) return
  if (event.key === 'Escape') {
    event.preventDefault()
    requestClose()
    return
  }
  if (event.key !== 'Tab' || !panelRef.value) return

  const focusable = Array.from(panelRef.value.querySelectorAll<HTMLElement>(
    'button:not([disabled]), select:not([disabled]), input:not([disabled]), [href], [tabindex]:not([tabindex="-1"])',
  )).filter((element) => !element.hasAttribute('hidden') && element.offsetParent !== null)
  if (focusable.length === 0) {
    event.preventDefault()
    panelRef.value.focus()
    return
  }

  const first = focusable[0]
  const last = focusable[focusable.length - 1]
  const active = document.activeElement
  const activeIsFocusable = active instanceof HTMLElement && focusable.includes(active)
  if (event.shiftKey && (active === first || !activeIsFocusable)) {
    event.preventDefault()
    last.focus()
  } else if (!event.shiftKey && (active === last || !activeIsFocusable)) {
    event.preventDefault()
    first.focus()
  }
}

function onScenarioChange(event: Event): void {
  selectScenario((event.target as HTMLSelectElement).value)
}

function setView(nextView: WorkspaceView): void {
  if (nextView === 'compare' && !canCompare.value) {
    announcement.value = comparisonInvariant.value.message
    return
  }
  view.value = nextView
}

async function copySeed(): Promise<void> {
  try {
    await navigator.clipboard.writeText(String(form.seed))
    announcement.value = `Seed ${form.seed} 已复制。`
  } catch {
    announcement.value = `Seed：${form.seed}。当前浏览器不允许自动写入剪贴板。`
  }
}

async function exportTrace(slot: RunSlot): Promise<void> {
  if (slot.phase !== 'success' || !slot.run?.ended_at) {
    announcement.value = `Run ${slot.side} 尚未 finalized，raw trace 暂不可导出。`
    return
  }
  try {
    const blob = await getRawTrace(slot.run.run_id)
    downloadBlobFile(buildTraceFilename(slot.run), blob)
    announcement.value = `Run ${slot.side} 已从服务端导出 byte-exact raw JSONL。`
  } catch (error) {
    const structured = toStructuredApiError(error)
    announcement.value = `导出失败 [${structured.code}] ${structured.message}`
  }
}

function exportBundle(): void {
  if (!slotA.run || !slotA.report || !slotB.run || !slotB.report) return
  const bundle = buildComparisonBundle({
    runA: slotA.run,
    reportA: slotA.report,
    eventsA: slotA.events,
    runB: slotB.run,
    reportB: slotB.report,
    eventsB: slotB.events,
  })
  downloadTextFile(
    buildComparisonFilename(slotA.run, slotB.run),
    `${JSON.stringify(bundle, null, 2)}\n`,
    'application/json;charset=utf-8',
  )
  announcement.value = 'A/B 对比包已导出；events 保持服务端 raw 形状并按 seq 排序。'
}

function runPolicy(side: RunSide): BaselinePolicy {
  return side === 'A' ? form.policyA : form.policyB
}

function policyDescription(side: RunSide): string {
  return policyOptions.find((option) => option.value === runPolicy(side))?.description ?? ''
}

function clearDeviceFilter(): void {
  uiStore.setActiveDevice(null)
  announcement.value = '已清除 3D 设备过滤。'
}

onMounted(() => window.addEventListener('keydown', onGlobalKeydown))
onBeforeUnmount(() => window.removeEventListener('keydown', onGlobalKeydown))
</script>

<template>
  <section
    ref="panelRef"
    class="research-workspace"
    role="dialog"
    aria-modal="true"
    aria-labelledby="research-title"
    :aria-hidden="!open"
    tabindex="-1"
  >
    <header class="workspace-header">
      <div class="workspace-brand">
        <p class="workspace-brand__eyebrow">AURA · EXPERIMENT WORKSPACE</p>
        <div class="workspace-brand__title-row">
          <h1 id="research-title">研究运行</h1>
          <span>CANONICAL SCENARIOS</span>
        </div>
        <p class="workspace-brand__copy">可复现实验入口；不同于“场景预设”中的设备快捷控制。</p>
      </div>

      <nav class="workspace-tabs" aria-label="研究运行步骤">
        <button
          v-for="tab in tabs"
          :key="tab.id"
          type="button"
          :class="{ active: view === tab.id }"
          :aria-current="view === tab.id ? 'step' : undefined"
          :disabled="tab.id === 'compare' && !canCompare"
          @click="setView(tab.id)"
        >
          <span>{{ tab.step }}</span>
          {{ tab.label }}
        </button>
      </nav>

      <button class="workspace-close" type="button" aria-label="关闭研究运行并返回 3D 场景" @click="requestClose">
        返回 3D
      </button>
    </header>

    <div class="workspace-status" aria-live="polite" aria-atomic="true">
      <span class="status-dot" :class="{ active: isBusy }" />
      <span>{{ announcement || '选择 canonical scenario，固定 seed，再运行两种 baseline policy。' }}</span>
    </div>

    <main ref="workspaceBodyRef" class="workspace-body">
      <div v-if="view === 'setup'" class="setup-view">
        <section class="setup-panel" aria-labelledby="setup-title">
          <header class="panel-heading">
            <div>
              <p>STEP 01 · EXPERIMENT CONTRACT</p>
              <h2 id="setup-title">固定场景与随机种子</h2>
            </div>
            <span class="contract-badge">SAME SCENARIO + SAME SEED</span>
          </header>

          <div v-if="scenariosResource.status === 'loading'" class="resource-state" role="status">
            <span class="loading-mark" />
            <div><strong>正在读取场景库</strong><p>GET /api/scenarios</p></div>
          </div>
          <div v-else-if="scenariosResource.status === 'error'" class="resource-state is-error" role="alert">
            <div>
              <strong>{{ scenariosResource.error?.code }}</strong>
              <p>{{ scenariosResource.error?.message }}</p>
            </div>
            <button type="button" @click="retryScenarios">重试</button>
          </div>
          <div v-else-if="scenarios.length === 0" class="resource-state">
            <div><strong>场景库为空</strong><p>请先在后端添加 canonical scenario。</p></div>
            <button type="button" @click="retryScenarios">刷新</button>
          </div>

          <div v-else class="experiment-form">
            <label class="form-field form-field--wide">
              <span class="form-field__label">Canonical scenario</span>
              <select :value="form.scenarioId" @change="onScenarioChange">
                <option v-for="scenario in scenarios" :key="scenario.id" :value="scenario.id">
                  {{ scenario.name }} · {{ scenario.id }}
                </option>
              </select>
              <small>这里运行完整评估场景；设备预设仍在右侧 “Scene Presets”。</small>
            </label>

            <label class="form-field">
              <span class="form-field__label">Seed</span>
              <span class="seed-control">
                <input v-model.number="form.seed" type="number" min="0" step="1" inputmode="numeric" />
                <button type="button" aria-label="复制 seed" @click="copySeed">复制</button>
              </span>
              <small>非负整数；A/B 比较的硬约束。</small>
            </label>

            <article v-if="selectedScenario" class="scenario-brief">
              <header>
                <div>
                  <p>{{ selectedScenario.id }}</p>
                  <h3>{{ selectedScenario.name }}</h3>
                </div>
                <span>SCHEMA {{ selectedScenario.scenario_schema_version }}</span>
              </header>
              <p>{{ selectedScenario.description }}</p>
              <dl>
                <div><dt>Duration</dt><dd>{{ selectedScenario.duration_seconds ?? 'auto' }} s</dd></div>
                <div><dt>Timeline</dt><dd>{{ selectedScenario.timeline_event_count }} events</dd></div>
                <div><dt>Effects</dt><dd>{{ selectedScenario.expected_device_effect_count }}</dd></div>
                <div><dt>Agents</dt><dd>{{ selectedScenario.involved_agents.join(', ') || '—' }}</dd></div>
              </dl>
            </article>
          </div>
        </section>

        <section class="policy-panel" aria-labelledby="policy-title">
          <header class="panel-heading">
            <div>
              <p>STEP 01 · BASELINE POLICIES</p>
              <h2 id="policy-title">先 A，后 B</h2>
            </div>
            <button
              class="copy-contract"
              type="button"
              :disabled="!slotA.config && !slotA.run"
              @click="copyAParameters"
            >
              复制 A 场景 + seed 到 B
            </button>
          </header>

          <div class="policy-grid">
            <article class="policy-column policy-column--a">
              <div class="policy-column__mark"><b>A</b><span /></div>
              <label class="form-field">
                <span class="form-field__label">Run A baseline policy</span>
                <select v-model="form.policyA">
                  <option v-for="option in policyOptions" :key="option.value" :value="option.value">
                    {{ option.label }} · {{ option.value }}
                  </option>
                </select>
                <small>{{ policyDescription('A') }}</small>
              </label>
              <label v-if="form.policyA === 'llm_recorded'" class="form-field">
                <span class="form-field__label">Recording source（可选）</span>
                <select v-model="form.recordingSourceA">
                  <option value="">留空：使用 live provider 首次录制</option>
                  <option v-for="run in recordingSourceRuns" :key="run.run_id" :value="run.run_id">
                    {{ run.scenario_id }} · {{ run.seed }} · {{ run.run_id }}
                  </option>
                </select>
                <small>选择已有 run 为零网络回放；留空需要服务端 live provider。</small>
              </label>
              <p v-if="form.policyA === 'llm_live'" class="live-warning">Live 会使用服务端凭据并可能产生费用。</p>
              <button class="launch-button" type="button" :disabled="!canLaunchA" @click="startSide('A')">
                {{ slotA.phase === 'empty' ? '启动 Run A' : '重新运行 A · 清空 B' }}
              </button>
            </article>

            <article class="policy-column policy-column--b">
              <div class="policy-column__mark"><b>B</b><span /></div>
              <label class="form-field">
                <span class="form-field__label">Run B baseline policy</span>
                <select v-model="form.policyB">
                  <option v-for="option in policyOptions" :key="option.value" :value="option.value">
                    {{ option.label }} · {{ option.value }}
                  </option>
                </select>
                <small>{{ policyDescription('B') }}</small>
              </label>
              <label v-if="form.policyB === 'llm_recorded'" class="form-field">
                <span class="form-field__label">Recording source（可选）</span>
                <select v-model="form.recordingSourceB">
                  <option value="">留空：使用 live provider 首次录制</option>
                  <option v-for="run in recordingSourceRuns" :key="run.run_id" :value="run.run_id">
                    {{ run.scenario_id }} · {{ run.seed }} · {{ run.run_id }}
                  </option>
                </select>
                <small>选择已有 run 为零网络回放；留空需要服务端 live provider。</small>
              </label>
              <p v-if="form.policyB === 'llm_live'" class="live-warning">Live 会使用服务端凭据并可能产生费用。</p>
              <p v-if="slotA.run && !bMatchesA" class="contract-warning" role="alert">
                当前场景或 seed 与 A 不同。先复制 A 参数，B 才能启动。
              </p>
              <button class="launch-button launch-button--b" type="button" :disabled="!canLaunchB" @click="startSide('B')">
                启动 Run B · 同条件重跑
              </button>
            </article>
          </div>

          <div v-if="runsResource.status === 'error'" class="catalog-warning" role="alert">
            <span>近期 run 列表不可用：{{ runsResource.error?.message }}</span>
            <button type="button" @click="refreshRecentRuns">重试</button>
          </div>
        </section>

        <div class="setup-runs">
          <ResearchRunCard :slot="slotA" @retry="retrySide('A')" @export="exportTrace(slotA)" />
          <ResearchRunCard :slot="slotB" @retry="retrySide('B')" @export="exportTrace(slotB)" />
        </div>
      </div>

      <div v-else-if="view === 'live'" class="live-view">
        <div class="live-runs">
          <ResearchRunCard :slot="slotA" @retry="retrySide('A')" @export="exportTrace(slotA)" />
          <ResearchRunCard :slot="slotB" @retry="retrySide('B')" @export="exportTrace(slotB)" />
        </div>

        <section class="live-observability" aria-labelledby="live-title">
          <header class="panel-heading">
            <div>
              <p>STEP 02 · LIVE CAUSAL STREAM</p>
              <h2 id="live-title">实时 Episode 链路</h2>
            </div>
            <div class="live-run-indicator">
              <span :class="{ active: activeSide }" />
              {{ activeSide ? `RUN ${activeSide} ACTIVE` : 'NO ACTIVE RUN' }}
            </div>
          </header>
          <p class="live-note">
            下方复用主界面的可观测面板，并响应 3D 设备点击过滤。结束后的完整 raw trace 请在 Compare 查看。
          </p>
          <ObservabilityPanel embedded class="embedded-observability" />
        </section>
      </div>

      <div v-else class="compare-view">
        <div v-if="!canCompare" class="compare-blocked" role="alert">
          <span class="compare-blocked__index">03</span>
          <div>
            <h2>对比尚未就绪</h2>
            <p>{{ comparisonInvariant.message }}</p>
          </div>
          <button type="button" @click="view = 'setup'">返回 Setup</button>
        </div>
        <template v-else>
          <div class="compare-provenance">
            <ResearchRunCard :slot="slotA" @retry="retrySide('A')" @export="exportTrace(slotA)" />
            <ResearchRunCard :slot="slotB" @retry="retrySide('B')" @export="exportTrace(slotB)" />
          </div>
          <RunComparison
            :slot-a="slotA"
            :slot-b="slotB"
            :active-device="activeDevice"
            @export-a="exportTrace(slotA)"
            @export-b="exportTrace(slotB)"
            @export-bundle="exportBundle"
            @clear-device-filter="clearDeviceFilter"
          />
        </template>
      </div>
    </main>
  </section>
</template>

<style scoped>
.research-workspace {
  position: absolute;
  inset: 0;
  z-index: calc(var(--z-modal) + 2);
  display: flex;
  flex-direction: column;
  pointer-events: auto;
  background: #07090d;
  border: 1px solid rgba(255, 255, 255, 0.1);
  color: var(--color-text-primary);
  box-shadow: -30px 0 60px rgba(0, 0, 0, 0.42);
  outline: none;
}

.workspace-header {
  min-height: 92px;
  padding: 14px 16px 14px 20px;
  display: grid;
  grid-template-columns: minmax(250px, 1fr) auto auto;
  align-items: center;
  gap: 18px;
  border-bottom: 1px solid rgba(255, 255, 255, 0.09);
  background: #0d1116;
}

.workspace-brand__eyebrow,
.workspace-brand h1,
.workspace-brand__copy {
  margin: 0;
}

.workspace-brand__eyebrow {
  font-size: 9px;
  letter-spacing: 0.19em;
  color: var(--color-primary);
}

.workspace-brand__title-row {
  margin-top: 5px;
  display: flex;
  align-items: baseline;
  gap: 10px;
}

.workspace-brand h1 {
  font-size: 21px;
  font-weight: 620;
  letter-spacing: -0.02em;
}

.workspace-brand__title-row span {
  font-family: ui-monospace, "SFMono-Regular", Menlo, monospace;
  font-size: 9px;
  color: var(--color-text-muted);
}

.workspace-brand__copy {
  margin-top: 5px;
  font-size: 10px;
  color: var(--color-text-secondary);
}

.workspace-tabs {
  display: flex;
  align-items: center;
  gap: 5px;
}

.workspace-tabs button,
.workspace-close,
.resource-state button,
.copy-contract,
.seed-control button,
.launch-button,
.catalog-warning button,
.compare-blocked button {
  min-height: 40px;
  border: 1px solid rgba(255, 255, 255, 0.09);
  border-radius: 6px;
  background: rgba(255, 255, 255, 0.025);
  color: var(--color-text-secondary);
  cursor: pointer;
  transition-property: border-color, color, background-color, transform, opacity;
  transition-duration: var(--transition-fast);
}

.workspace-tabs button {
  min-width: 88px;
  padding: 0 12px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  font-size: 11px;
}

.workspace-tabs button span {
  font-family: ui-monospace, "SFMono-Regular", Menlo, monospace;
  font-size: 9px;
  color: var(--color-text-muted);
}

.workspace-tabs button.active {
  border-color: rgba(255, 231, 74, 0.36);
  background: rgba(255, 231, 74, 0.09);
  color: var(--color-primary);
}

.workspace-tabs button:disabled,
.copy-contract:disabled,
.launch-button:disabled {
  opacity: 0.36;
  cursor: not-allowed;
}

.workspace-close {
  min-width: 84px;
  padding: 0 12px;
}

.workspace-tabs button:active:not(:disabled),
.workspace-close:active,
.resource-state button:active,
.copy-contract:active:not(:disabled),
.seed-control button:active,
.launch-button:active:not(:disabled),
.catalog-warning button:active,
.compare-blocked button:active {
  transform: scale(0.96);
}

.workspace-tabs button:focus-visible,
.workspace-close:focus-visible,
.resource-state button:focus-visible,
.copy-contract:focus-visible,
.seed-control button:focus-visible,
.launch-button:focus-visible,
.catalog-warning button:focus-visible,
.compare-blocked button:focus-visible,
.form-field select:focus-visible,
.form-field input:focus-visible {
  outline: 2px solid var(--color-primary);
  outline-offset: 2px;
}

.workspace-status {
  min-height: 34px;
  padding: 0 20px;
  display: flex;
  align-items: center;
  gap: 8px;
  border-bottom: 1px solid rgba(255, 255, 255, 0.07);
  background: #090c10;
  font-size: 10px;
  color: var(--color-text-secondary);
}

.status-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: rgba(255, 255, 255, 0.2);
}

.status-dot.active {
  background: var(--color-primary);
  box-shadow: 0 0 0 4px rgba(255, 231, 74, 0.08);
  animation: status-pulse 1.4s ease-in-out infinite;
}

.workspace-body {
  min-height: 0;
  flex: 1;
  overflow-y: auto;
  background: #07090d;
}

.setup-view,
.live-view,
.compare-view {
  padding: 14px;
  display: grid;
  gap: 14px;
}

.setup-panel,
.policy-panel,
.live-observability {
  border: 1px solid rgba(255, 255, 255, 0.08);
  border-radius: 6px;
  background: #0a0d12;
  overflow: hidden;
}

.panel-heading {
  min-height: 62px;
  padding: 12px 14px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 14px;
  border-bottom: 1px solid rgba(255, 255, 255, 0.07);
  background: #10141a;
}

.panel-heading p,
.panel-heading h2 {
  margin: 0;
}

.panel-heading p {
  font-size: 9px;
  letter-spacing: 0.15em;
  color: var(--color-text-muted);
}

.panel-heading h2 {
  margin-top: 5px;
  font-size: 16px;
  font-weight: 600;
}

.contract-badge,
.live-run-indicator {
  min-height: 30px;
  padding: 0 9px;
  display: inline-flex;
  align-items: center;
  gap: 7px;
  border: 1px solid rgba(255, 231, 74, 0.24);
  border-radius: 2px;
  font-family: ui-monospace, "SFMono-Regular", Menlo, monospace;
  font-size: 9px;
  color: var(--color-primary);
}

.resource-state {
  min-height: 140px;
  padding: 22px;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 14px;
  text-align: left;
  color: var(--color-text-secondary);
}

.resource-state strong,
.resource-state p {
  margin: 0;
}

.resource-state strong {
  font-size: 13px;
  color: var(--color-text-primary);
}

.resource-state p {
  margin-top: 5px;
  font-size: 10px;
}

.resource-state.is-error {
  color: var(--color-danger);
}

.resource-state button {
  padding: 0 12px;
}

.loading-mark {
  width: 22px;
  height: 22px;
  border: 2px solid rgba(255, 255, 255, 0.1);
  border-top-color: var(--color-primary);
  border-radius: 50%;
  animation: workspace-spin 0.9s linear infinite;
}

.experiment-form {
  padding: 14px;
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(180px, 0.35fr);
  gap: 12px;
}

.form-field {
  min-width: 0;
  display: grid;
  align-content: start;
  gap: 7px;
}

.form-field__label {
  font-size: 9px;
  letter-spacing: 0.13em;
  text-transform: uppercase;
  color: var(--color-text-muted);
}

.form-field select,
.form-field input {
  width: 100%;
  min-height: 42px;
  padding: 0 11px;
  border: 1px solid rgba(255, 255, 255, 0.1);
  border-radius: 6px;
  background: #0d1116;
  color: var(--color-text-primary);
}

.form-field small {
  min-height: 30px;
  font-size: 10px;
  line-height: 1.45;
  color: var(--color-text-muted);
}

.seed-control {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
}

.seed-control input {
  border-radius: 6px 0 0 6px;
}

.seed-control button {
  min-width: 64px;
  border-radius: 0 6px 6px 0;
  border-left: 0;
}

.scenario-brief {
  grid-column: 1 / -1;
  padding: 13px;
  border: 1px solid rgba(255, 255, 255, 0.07);
  border-radius: 2px;
  background: #0c1015;
}

.scenario-brief header {
  display: flex;
  justify-content: space-between;
  gap: 12px;
}

.scenario-brief p,
.scenario-brief h3,
.scenario-brief dl,
.scenario-brief dt,
.scenario-brief dd {
  margin: 0;
}

.scenario-brief header p,
.scenario-brief header span {
  font-family: ui-monospace, "SFMono-Regular", Menlo, monospace;
  font-size: 9px;
  color: var(--color-primary);
}

.scenario-brief h3 {
  margin-top: 4px;
  font-size: 14px;
  font-weight: 600;
}

.scenario-brief > p {
  margin-top: 8px;
  font-size: 11px;
  line-height: 1.6;
  color: var(--color-text-secondary);
}

.scenario-brief dl {
  margin-top: 11px;
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 1px;
  background: rgba(255, 255, 255, 0.06);
}

.scenario-brief dl > div {
  min-width: 0;
  padding: 8px;
  background: #0a0d12;
}

.scenario-brief dt {
  font-size: 8px;
  letter-spacing: 0.1em;
  text-transform: uppercase;
  color: var(--color-text-muted);
}

.scenario-brief dd {
  margin-top: 4px;
  overflow-wrap: anywhere;
  font-size: 10px;
  color: var(--color-text-secondary);
}

.copy-contract {
  padding: 0 12px;
}

.policy-grid,
.setup-runs,
.live-runs,
.compare-provenance {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px;
}

.policy-grid {
  padding: 14px;
}

.policy-column {
  min-width: 0;
  padding: 13px;
  display: grid;
  align-content: start;
  gap: 11px;
  border: 1px solid rgba(255, 255, 255, 0.08);
  border-radius: 6px;
  background: #0c1015;
}

.policy-column__mark {
  display: flex;
  align-items: center;
  gap: 8px;
  color: var(--color-primary);
}

.policy-column__mark b {
  font-family: ui-monospace, "SFMono-Regular", Menlo, monospace;
  font-size: 17px;
}

.policy-column__mark span {
  width: 50px;
  height: 2px;
  background: var(--color-primary);
}

.policy-column--b .policy-column__mark span {
  background: repeating-linear-gradient(90deg, var(--color-primary) 0 5px, transparent 5px 9px);
}

.live-warning,
.contract-warning {
  margin: 0;
  padding: 9px 10px;
  border: 1px solid rgba(248, 113, 113, 0.19);
  border-radius: 2px;
  background: rgba(248, 113, 113, 0.06);
  font-size: 10px;
  line-height: 1.5;
  color: #fca5a5;
}

.contract-warning {
  border-color: rgba(255, 231, 74, 0.2);
  background: rgba(255, 231, 74, 0.06);
  color: var(--color-primary);
}

.launch-button {
  width: 100%;
  padding: 0 14px;
  border-color: rgba(255, 231, 74, 0.35);
  background: rgba(255, 231, 74, 0.09);
  color: var(--color-primary);
  font-weight: 600;
}

.launch-button--b {
  border-style: dashed;
}

.catalog-warning {
  margin: 0 14px 14px;
  padding: 9px 10px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  border: 1px solid rgba(248, 113, 113, 0.18);
  border-radius: 2px;
  font-size: 10px;
  color: #fca5a5;
}

.catalog-warning button {
  padding: 0 10px;
}

.live-view {
  grid-template-rows: auto minmax(680px, 1fr);
}

.live-observability {
  min-height: 680px;
  display: flex;
  flex-direction: column;
}

.live-run-indicator span {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: rgba(255, 255, 255, 0.2);
}

.live-run-indicator span.active {
  background: var(--color-primary);
}

.live-note {
  margin: 0;
  padding: 9px 14px;
  border-bottom: 1px solid rgba(255, 255, 255, 0.07);
  font-size: 10px;
  line-height: 1.5;
  color: var(--color-text-secondary);
}

.embedded-observability {
  min-height: 0;
  flex: 1;
  border: 0;
  border-radius: 0;
}

.compare-blocked {
  min-height: 300px;
  padding: 30px;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 18px;
  border: 1px dashed rgba(255, 255, 255, 0.12);
  border-radius: 6px;
  background: #0a0d12;
}

.compare-blocked__index {
  font-family: ui-monospace, "SFMono-Regular", Menlo, monospace;
  font-size: 38px;
  color: var(--color-primary);
}

.compare-blocked h2,
.compare-blocked p {
  margin: 0;
}

.compare-blocked h2 {
  font-size: 17px;
}

.compare-blocked p {
  margin-top: 7px;
  max-width: 500px;
  font-size: 11px;
  line-height: 1.6;
  color: var(--color-text-secondary);
}

.compare-blocked button {
  padding: 0 13px;
}

@media (min-width: 1600px) {
  .research-workspace {
    top: 20px;
    right: 20px;
    bottom: 20px;
    left: auto;
    width: min(1360px, calc(100vw - 400px));
    border-radius: 12px;
    overflow: hidden;
  }
}

@media (max-width: 980px) {
  .workspace-header {
    grid-template-columns: 1fr auto;
  }

  .workspace-tabs {
    grid-column: 1 / -1;
    grid-row: 2;
  }

  .workspace-tabs button {
    flex: 1;
  }

  .experiment-form,
  .policy-grid,
  .setup-runs,
  .live-runs,
  .compare-provenance {
    grid-template-columns: 1fr;
  }
}

@media (max-width: 640px) {
  .workspace-header {
    min-height: auto;
    padding: 12px;
    gap: 10px;
  }

  .workspace-brand__copy,
  .workspace-brand__title-row span {
    display: none;
  }

  .workspace-tabs {
    width: 100%;
  }

  .workspace-tabs button {
    min-width: 0;
    padding: 0 7px;
  }

  .workspace-status {
    padding: 0 12px;
  }

  .setup-view,
  .live-view,
  .compare-view {
    padding: 8px;
  }

  .panel-heading {
    align-items: flex-start;
    flex-direction: column;
  }

  .contract-badge,
  .copy-contract {
    width: 100%;
  }

  .experiment-form {
    grid-template-columns: 1fr;
  }

  .scenario-brief dl {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .compare-blocked {
    align-items: stretch;
    flex-direction: column;
  }
}

@media (hover: hover) {
  .workspace-tabs button:hover:not(:disabled),
  .workspace-close:hover,
  .resource-state button:hover,
  .copy-contract:hover:not(:disabled),
  .seed-control button:hover,
  .catalog-warning button:hover,
  .compare-blocked button:hover {
    border-color: rgba(255, 231, 74, 0.35);
    color: var(--color-text-primary);
  }

  .launch-button:hover:not(:disabled) {
    background: rgba(255, 231, 74, 0.14);
  }
}

@keyframes workspace-spin {
  to { transform: rotate(360deg); }
}

@keyframes status-pulse {
  50% { opacity: 0.35; }
}

@media (prefers-reduced-motion: reduce) {
  .workspace-tabs button,
  .workspace-close,
  .resource-state button,
  .copy-contract,
  .seed-control button,
  .launch-button,
  .catalog-warning button,
  .compare-blocked button {
    transition-duration: 0.01ms;
  }

  .loading-mark,
  .status-dot.active {
    animation: none;
  }
}
</style>
