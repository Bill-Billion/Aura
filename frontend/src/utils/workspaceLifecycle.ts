/** Keep keyboard focus inside the opened workspace before catalog I/O can block. */
export async function focusBeforeInitialize(
  focus: () => void,
  initialize: () => Promise<void>,
): Promise<void> {
  focus()
  await initialize()
}
