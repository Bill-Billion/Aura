export function normalizeSceneNodeName(value: string): string {
  return value
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9_-]/g, '')
}

export function normalizeSceneNodeNames(values: string[]): string[] {
  return [...new Set(values.map(normalizeSceneNodeName).filter(Boolean))]
}
