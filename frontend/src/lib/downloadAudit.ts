import { sessions } from '../api/client'

/**
 * Fetch the run evidence package for a session and trigger a JSON
 * download. Shared by the chat header button and the developer Run tab.
 * Throws on failure — callers decide how to surface the error.
 */
export async function downloadRunAudit(sessionId: string): Promise<void> {
  const audit = await sessions.getAudit(sessionId)
  const blob = new Blob([JSON.stringify(audit, null, 2)], {
    type: 'application/json',
  })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `orcha-run-audit-${sessionId}.json`
  a.click()
  URL.revokeObjectURL(url)
}
