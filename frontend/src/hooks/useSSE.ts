import { useCallback, useRef } from 'react'
import type { ChecklistTaskStatus, InterruptEvent, SSEEvent } from '../types'
import { useSessionStore } from '../store/session'
import { mergeAgentsAdditive } from '../lib/sessionDetailsReducer'


function parseLine(line: string): SSEEvent | null {
  if (!line.startsWith('data:')) return null
  try {
    return JSON.parse(line.slice(5).trim()) as SSEEvent
  } catch {
    return null
  }
}

const CHECKLIST_STATUS: Record<string, ChecklistTaskStatus> = {
  pending: 'pending',
  in_progress: 'running',
  done: 'done',
  failed: 'failed',
}

export function useSSE() {
  const readerRef = useRef<ReadableStreamDefaultReader<Uint8Array> | null>(null)
  // Per-stream seen-checklist set — keyed on readerRef object to scope to one stream
  const seenChecklistsRef = useRef<Set<string>>(new Set())
  const store = useSessionStore()

  const handleEvent = useCallback(
    (event: SSEEvent) => {
      const s = useSessionStore.getState()
      // Developer Trace tab: record every event from this single stream.
      s.recordTraceEvent(event.type)
      switch (event.type) {
        case 'token': {
          if (!s.streamingMessageId) {
            s.beginStreaming()
          }
          s.appendToken(event.content)
          break
        }
        case 'invocation_start': {
          // Mark any streaming message as orchestrator reasoning (thinking block)
          s.endStreaming(true)
          s.toolTraceStart({
            call_id: event.call_id,
            tool_name: event.tool_name,
            agent_id: event.agent_id,
            inputs: event.inputs,
            protocol: event.protocol,
          })
          if (event.agent_id && !event.agent_id.startsWith('_')) {
            s.updateAgent(event.agent_id, { status: 'running' })
          }
          break
        }
        case 'invocation_progress': {
          if (event.message) {
            s.toolTraceProgress(event.call_id, event.message)
          }
          break
        }
        case 'invocation_retry': {
          s.toolTraceRetry({
            call_id: event.call_id,
            attempt: event.attempt,
            max_attempts: event.max_attempts,
            reason: event.reason,
          })
          break
        }
        case 'invocation_result': {
          s.toolTraceFinalize({
            call_id: event.call_id,
            tool_name: event.tool_name,
            agent_id: event.agent_id,
            status: event.status,
            content_preview: event.content_preview,
            total_cost_usd: event.total_cost_usd,
            base_fee: event.base_fee,
            verified: event.verified,
            verdict_reason: event.verdict_reason,
          })
          if (event.agent_id && !event.agent_id.startsWith('_')) {
            s.updateAgent(event.agent_id, { status: 'done' })
          }
          break
        }
        case 'agents_discovered': {
          // SA sends { agent_id, agent_name, protocol_type } — map to store's AgentInfo
          const incoming = event.agents.map((a) => {
            const protocol = (a.protocol_type ?? '').toLowerCase()
            return {
              agent_id: a.agent_id,
              name: a.agent_name || a.agent_id,
              type: (
                protocol === 'mcp' ? 'mcp' : protocol === 'a2a' ? 'a2a' : 'native'
              ) as 'mcp' | 'a2a' | 'native',
              status: 'pending' as const,
            }
          })
          const merged = mergeAgentsAdditive(s.agents, incoming)
          s.setAgents(merged)
          if (incoming.length > 0) {
            s.appendSessionLog(
              `Agents discovered: ${incoming.map((a) => a.name).join(', ')}`,
            )
          }
          break
        }
        case 'token_usage': {
          // SA sends estimated_token_count (no input/output split)
          s.mergeTokenUsage({
            input: undefined,
            output: undefined,
            total: event.estimated_token_count,
          })
          break
        }
        case 'artifact_created': {
          const label =
            event.filename ||
            event.description ||
            event.artifact_id
          s.addArtifact({
            artifact_id: event.artifact_id,
            name: label,
            type: event.mime_type || 'unknown',
          })
          s.appendSessionLog(`Artifact: ${label}`)
          break
        }
        case 'interrupt': {
          s.endStreaming()
          const ie = event as InterruptEvent
          s.addInterrupt({
            interrupt_id: ie.interrupt_id,
            interrupt_type: ie.interrupt_type,
            message: ie.message,
            metadata: ie.metadata,
          })
          s.setStatus('interrupted')
          break
        }
        case 'done': {
          s.endStreaming()
          s.setStatus('complete')
          s.appendSessionLog('Turn complete')
          if (event.run_id && event.attestation_path) {
            s.attachReceiptToLastTurn({
              runId: event.run_id,
              attestationPath: event.attestation_path,
            })
          }
          break
        }
        case 'stopped': {
          s.endStreaming()
          s.setStatus('idle')
          s.appendSessionLog('Execution stopped by user')
          break
        }
        case 'checklist_snapshot': {
          const tasks = event.steps.map((step) => ({
            id: step.step_id,
            label: step.description,
            status: CHECKLIST_STATUS[step.status] ?? 'pending',
          }))
          const isNew = !seenChecklistsRef.current.has(event.checklist_id)
          if (isNew) {
            seenChecklistsRef.current.add(event.checklist_id)
            s.setChecklist(tasks)
          } else {
            s.updateChecklist(tasks)
          }
          break
        }
        case 'error': {
          s.endStreaming()
          s.setStatus('failed')
          s.appendSessionLog(`Error: ${event.error ?? 'unknown'}`)
          s.addMessage({
            id: crypto.randomUUID(),
            role: 'error',
            content: event.error ?? 'An unexpected error occurred',
            timestamp: Date.now(),
          })
          break
        }
        case 'auth_complete': {
          // Gateway signals that an OAuth callback completed.
          // Status will be updated via the resumed SSE stream.
          s.appendSessionLog(`Auth complete: ${event.interrupt_type}`)
          break
        }
        case 'canvas_manifest': {
          const seq = useSessionStore.getState().timelineSeq + 1
          s.addCanvas({ id: event.manifest_id, manifest: event.manifest, sortIndex: seq })
          break
        }
      }
    },
    [],
  )

  const streamResponse = useCallback(
    async (response: Response) => {
      if (!response.body) return
      store.setStatus('running')
      // Reset per-stream checklist tracking for the new stream
      seenChecklistsRef.current = new Set()

      const decoder = new TextDecoder()
      const reader = response.body.getReader()
      readerRef.current = reader
      let buffer = ''

      try {
        while (true) {
          const { done, value } = await reader.read()
          if (done) break

          buffer += decoder.decode(value, { stream: true })
          const lines = buffer.split('\n')
          buffer = lines.pop() ?? ''

          for (const line of lines) {
            const event = parseLine(line.trim())
            if (event) handleEvent(event)
          }
        }
      } finally {
        reader.releaseLock()
        readerRef.current = null
        const currentStatus = useSessionStore.getState().status
        if (currentStatus === 'running') {
          store.endStreaming()
          store.setStatus('idle')
        }
      }
    },
    [handleEvent, store],
  )

  const abort = useCallback(() => {
    readerRef.current?.cancel()
  }, [])

  return { streamResponse, abort }
}
