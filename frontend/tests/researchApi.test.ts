import { createAbortableResource, createResearchApi } from '../src/composables/researchApi.ts'

test('abortable resource 覆盖 idle/loading/success/error 四态', async () => {
  const resource = createAbortableResource<string>()
  expect(resource.state.value.status).toBe('idle')

  let resolveValue: ((value: string) => void) | null = null
  const pending = resource.load(() => new Promise<string>((resolve) => {
    resolveValue = resolve
  }))
  expect(resource.state.value.status).toBe('loading')
  resolveValue?.('ready')
  await pending
  expect(resource.state.value).toMatchObject({ status: 'success', data: 'ready', error: null })

  await resource.load(async () => {
    throw new Error('offline')
  })
  expect(resource.state.value.status).toBe('error')
  expect(resource.state.value.error).toMatchObject({ code: 'network_error', message: 'offline' })
})

test('新请求会 abort 旧请求，旧结果不会覆盖最新 success', async () => {
  const resource = createAbortableResource<string>()
  let firstSignal: AbortSignal | null = null
  let resolveFirst: ((value: string) => void) | null = null
  const first = resource.load((signal) => {
    firstSignal = signal
    return new Promise<string>((resolve) => {
      resolveFirst = resolve
    })
  })
  const second = resource.load(async () => 'second')
  expect(firstSignal?.aborted).toBe(true)
  resolveFirst?.('stale-first')

  await Promise.all([first, second])
  expect(resource.state.value).toMatchObject({ status: 'success', data: 'second' })
})

test('research API 保留结构化错误并只给 recorded payload 带 source run', async () => {
  const requests: Array<{ input: string; init?: RequestInit }> = []
  const api = createResearchApi(async (input, init) => {
    requests.push({ input: String(input), init })
    if (String(input).endsWith('/scenarios')) {
      return new Response(JSON.stringify({
        detail: {
          code: 'scenario_library_invalid',
          message: '场景库损坏',
          details: { path: 'bad.yaml' },
        },
      }), { status: 500, headers: { 'Content-Type': 'application/json' } })
    }
    return new Response(JSON.stringify({ run: { run_id: 'run-a' } }), {
      status: 201,
      headers: { 'Content-Type': 'application/json' },
    })
  })

  await expect(api.listScenarios()).rejects.toMatchObject({
    info: {
      code: 'scenario_library_invalid',
      message: '场景库损坏',
      status: 500,
      details: { path: 'bad.yaml' },
    },
  })
  await api.startRun({
    scenario_id: 'morning_wake_up',
    seed: 1004,
    baseline_policy: 'llm_recorded',
    idempotency_key: '11111111-1111-4111-8111-111111111111',
    recording_source_run_id: 'source-run',
  })
  const body = JSON.parse(String(requests.at(-1)?.init?.body))
  expect(body).toEqual({
    scenario_id: 'morning_wake_up',
    seed: 1004,
    baseline_policy: 'llm_recorded',
    idempotency_key: '11111111-1111-4111-8111-111111111111',
    recording_source_run_id: 'source-run',
  })
})

test('单侧 raw trace 直接请求服务端 attachment，不经前端事件投影', async () => {
  const requested: string[] = []
  const api = createResearchApi(async (input) => {
    requested.push(String(input))
    return new Response('{"seq":0}\n', {
      status: 200,
      headers: { 'Content-Type': 'application/x-ndjson' },
    })
  })

  const blob = await api.getRawTrace('run/with spaces')
  expect(requested).toEqual(['/api/runs/run%2Fwith%20spaces/events?format=raw'])
  expect(await blob.text()).toBe('{"seq":0}\n')
})
