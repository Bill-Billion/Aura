import { readonly, shallowRef, type DeepReadonly, type Ref } from 'vue'
import type { EvalReport } from '@/types/eval-report'
import type {
  RawRunEvent,
  RemoteState,
  RunEnvelope,
  RunEventsEnvelope,
  RunLaunchConfig,
  RunListEnvelope,
  ScenarioListEnvelope,
  StartRunEnvelope,
  StructuredApiError,
} from '@/types/research-run'
import { sortRawEvents } from '../utils/runComparison'

const API_BASE = '/api'
const EVENT_PAGE_SIZE = 5000

type FetchLike = (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>

export class ResearchApiError extends Error {
  readonly info: StructuredApiError

  constructor(info: StructuredApiError) {
    super(info.message)
    this.name = 'ResearchApiError'
    this.info = info
  }
}

export interface AbortableResource<T> {
  state: DeepReadonly<Ref<RemoteState<T>>>
  load: (loader: (signal: AbortSignal) => Promise<T>) => Promise<T | null>
  abort: () => void
}

export function createAbortableResource<T>(): AbortableResource<T> {
  const state = shallowRef<RemoteState<T>>({ status: 'idle', data: null, error: null })
  let controller: AbortController | null = null
  let generation = 0

  async function load(loader: (signal: AbortSignal) => Promise<T>): Promise<T | null> {
    controller?.abort()
    controller = new AbortController()
    const currentController = controller
    const requestGeneration = ++generation
    state.value = { status: 'loading', data: state.value.data, error: null }

    try {
      const data = await loader(currentController.signal)
      if (requestGeneration !== generation || currentController.signal.aborted) return null
      state.value = { status: 'success', data, error: null }
      controller = null
      return data
    } catch (error) {
      if (requestGeneration !== generation || currentController.signal.aborted) return null
      const structured = toStructuredApiError(error)
      state.value = { status: 'error', data: state.value.data, error: structured }
      controller = null
      return null
    }
  }

  function abort(): void {
    generation += 1
    controller?.abort()
    controller = null
    if (state.value.status === 'loading') {
      state.value = { status: 'idle', data: state.value.data, error: null }
    }
  }

  return { state: readonly(state), load, abort }
}

export function createResearchApi(fetcher: FetchLike = fetch) {
  async function getJson<T>(path: string, signal?: AbortSignal, init: RequestInit = {}): Promise<T> {
    const response = await fetcher(`${API_BASE}${path}`, { ...init, signal })
    if (!response.ok) throw new ResearchApiError(await responseError(response))
    return response.json() as Promise<T>
  }

  return {
    listScenarios(signal?: AbortSignal): Promise<ScenarioListEnvelope> {
      return getJson('/scenarios', signal)
    },

    listRuns(signal?: AbortSignal): Promise<RunListEnvelope> {
      return getJson('/runs?limit=100', signal)
    },

    startRun(config: RunLaunchConfig, signal?: AbortSignal): Promise<StartRunEnvelope> {
      return getJson('/runs', signal, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(config),
      })
    },

    getRun(runId: string, signal?: AbortSignal): Promise<RunEnvelope> {
      return getJson(`/runs/${encodeURIComponent(runId)}`, signal)
    },

    getReport(runId: string, signal?: AbortSignal): Promise<EvalReport> {
      return getJson(`/runs/${encodeURIComponent(runId)}/report`, signal)
    },

    async getRawTrace(runId: string, signal?: AbortSignal): Promise<Blob> {
      const response = await fetcher(
        `${API_BASE}/runs/${encodeURIComponent(runId)}/events?format=raw`,
        { signal },
      )
      if (!response.ok) throw new ResearchApiError(await responseError(response))
      return response.blob()
    },

    async getEvents(runId: string, signal?: AbortSignal): Promise<RawRunEvent[]> {
      const events: RawRunEvent[] = []
      let offset = 0
      let total = Number.POSITIVE_INFINITY

      while (offset < total) {
        const page = await getJson<RunEventsEnvelope>(
          `/runs/${encodeURIComponent(runId)}/events?offset=${offset}&limit=${EVENT_PAGE_SIZE}`,
          signal,
        )
        events.push(...page.events)
        total = page.total
        if (page.events.length === 0) break
        offset += page.events.length
      }
      return sortRawEvents(events)
    },
  }
}

export function toStructuredApiError(error: unknown): StructuredApiError {
  if (error instanceof ResearchApiError) return error.info
  if (error instanceof DOMException && error.name === 'AbortError') {
    return { code: 'request_aborted', message: '请求已取消', status: null, details: null }
  }
  if (error instanceof Error) {
    return { code: 'network_error', message: error.message, status: null, details: null }
  }
  return { code: 'unknown_error', message: String(error), status: null, details: null }
}

export function abortableDelay(milliseconds: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal.aborted) {
      reject(new DOMException('Aborted', 'AbortError'))
      return
    }
    const timeout = window.setTimeout(resolve, milliseconds)
    signal.addEventListener('abort', () => {
      window.clearTimeout(timeout)
      reject(new DOMException('Aborted', 'AbortError'))
    }, { once: true })
  })
}

async function responseError(response: Response): Promise<StructuredApiError> {
  let body: unknown = null
  try {
    body = await response.json()
  } catch {
    body = null
  }
  const detail = isRecord(body) ? body.detail : null
  const source = isRecord(detail) ? detail : isRecord(body) ? body : null
  return {
    code: typeof source?.code === 'string' ? source.code : `http_${response.status}`,
    message: typeof source?.message === 'string'
      ? source.message
      : typeof detail === 'string'
        ? detail
        : `请求失败（HTTP ${response.status}）`,
    status: response.status,
    details: isRecord(source?.details)
      ? source.details
      : Array.isArray(detail)
        ? { issues: detail }
        : null,
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}
