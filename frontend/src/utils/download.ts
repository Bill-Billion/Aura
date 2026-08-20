export function downloadTextFile(filename: string, content: string, mimeType: string): void {
  downloadBlobFile(filename, new Blob([content], { type: mimeType }))
}

export function downloadBlobFile(filename: string, blob: Blob): void {
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = filename
  anchor.hidden = true
  document.body.appendChild(anchor)
  anchor.click()
  anchor.remove()
  URL.revokeObjectURL(url)
}
