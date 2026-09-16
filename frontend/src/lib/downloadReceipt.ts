import { getAccessToken, tryRefresh } from '../api/client'

const BASE_URL = import.meta.env.VITE_GATEWAY_URL ?? 'http://localhost:8080'

/**
 * Fetch the sealed RFC 0003 envelope for a run and save it as ``<runId>.json``.
 * Bytes are written as received — never parsed or re-serialized. This is not
 * the audit package (see downloadAudit.ts) and the file is not marked verified.
 */
export async function downloadReceipt(runId: string): Promise<void> {
  const fetchOnce = async (): Promise<Response> => {
    const headers: Record<string, string> = {}
    const token = getAccessToken()
    if (token) headers.Authorization = `Bearer ${token}`
    return fetch(
      `${BASE_URL}/api/v1/runs/${encodeURIComponent(runId)}/attestation`,
      { method: 'GET', headers },
    )
  }

  let res = await fetchOnce()
  if (res.status === 401) {
    const refreshed = await tryRefresh()
    if (refreshed) res = await fetchOnce()
  }
  if (!res.ok) {
    const text = await res.text().catch(() => res.statusText)
    throw new Error(`${res.status}: ${text}`)
  }

  const bytes = await res.arrayBuffer()
  const blob = new Blob([bytes], { type: 'application/json' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `${runId}.json`
  a.click()
  URL.revokeObjectURL(url)
}
