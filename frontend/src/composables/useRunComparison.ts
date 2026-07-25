import { ref } from 'vue'
import type { EvalReport, RunSummary } from '@/types/eval-report'

const API_BASE = '/api'

export function useRunComparison() {
  const runs = ref<RunSummary[]>([])
  const runsLoading = ref(false)
  const reportA = ref<EvalReport | null>(null)
  const reportB = ref<EvalReport | null>(null)
  const loadingA = ref(false)
  const loadingB = ref(false)
  const errorA = ref<string | null>(null)
  const errorB = ref<string | null>(null)
  const runIdA = ref('')
  const runIdB = ref('')

  async function fetchRuns() {
    runsLoading.value = true
    try {
      const resp = await fetch(`${API_BASE}/runs?limit=20`)
      const data = await resp.json()
      runs.value = data.runs || []
    } catch (e) {
      console.error('Failed to fetch runs:', e)
    } finally {
      runsLoading.value = false
    }
  }

  async function fetchReport(runId: string, side: 'A' | 'B') {
    const loading = side === 'A' ? loadingA : loadingB
    const error = side === 'A' ? errorA : errorB
    const report = side === 'A' ? reportA : reportB

    loading.value = true
    error.value = null
    try {
      const resp = await fetch(`${API_BASE}/runs/${runId}/report`)
      if (!resp.ok) {
        const errData = await resp.json().catch(() => ({}))
        throw new Error(errData.detail?.message || `HTTP ${resp.status}`)
      }
      const data = await resp.json()
      report.value = data as EvalReport
    } catch (e) {
      error.value = e instanceof Error ? e.message : String(e)
      report.value = null
    } finally {
      loading.value = false
    }
  }

  function selectRunA(runId: string) {
    runIdA.value = runId
    if (runId) fetchReport(runId, 'A')
  }

  function selectRunB(runId: string) {
    runIdB.value = runId
    if (runId) fetchReport(runId, 'B')
  }

  async function compareRuns(runA: string, runB: string) {
    selectRunA(runA)
    selectRunB(runB)
  }

  return {
    runs,
    runsLoading,
    reportA,
    reportB,
    loadingA,
    loadingB,
    errorA,
    errorB,
    runIdA,
    runIdB,
    fetchRuns,
    selectRunA,
    selectRunB,
    compareRuns,
  }
}
