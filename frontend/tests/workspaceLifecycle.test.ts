import { focusBeforeInitialize } from '../src/utils/workspaceLifecycle.ts'

test('workspace 在 deferred catalog fetch 完成前先迁移焦点', async () => {
  let focused = false
  let resolveFetch: (() => void) | null = null
  const deferredFetch = new Promise<void>((resolve) => {
    resolveFetch = resolve
  })

  const opening = focusBeforeInitialize(
    () => { focused = true },
    () => deferredFetch,
  )
  expect(focused).toBe(true)

  let completed = false
  opening.then(() => { completed = true })
  await Promise.resolve()
  expect(completed).toBe(false)
  resolveFetch?.()
  await opening
  expect(completed).toBe(true)
})
