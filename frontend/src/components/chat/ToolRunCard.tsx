import { useEffect, useRef, useState } from 'react'
import type { ToolInvocationPhase, ToolInvocationTrace } from '../../types'
import { cn } from '../ui/cn'

interface ToolRunCardProps {
  readonly trace: ToolInvocationTrace
}

function phaseLabel(phase: ToolInvocationPhase): string {
  if (phase === 'running') return 'Running'
  if (phase === 'error') return 'Failed'
  return 'Done'
}

const PROTOCOL_BADGE: Record<string, { label: string; className: string }> = {
  mcp:          { label: 'MCP',          className: 'border-blue-500/40 bg-blue-500/10 text-blue-400' },
  a2a:          { label: 'A2A',          className: 'border-purple-500/40 bg-purple-500/10 text-purple-400' },
  computer_use: { label: 'COMPUTER_USE', className: 'border-amber-500/40 bg-amber-500/10 text-amber-400' },
  acp:          { label: 'ACP',          className: 'border-green-500/40 bg-green-500/10 text-green-400' },
}

function VerifiedBadge({ verified, reason }: { verified?: boolean; reason?: string }) {
  if (verified === undefined) return null
  if (verified) {
    return (
      <span
        className="shrink-0 rounded-sm border border-semantic-success/40 bg-semantic-successDim px-2 py-0.5 text-caption font-semibold text-semantic-success"
        title={reason || 'Structural check passed'}
      >
        Structurally checked
      </span>
    )
  }
  return (
    <span
      className="shrink-0 rounded-sm border border-semantic-error/40 bg-semantic-errorDim px-2 py-0.5 text-caption font-semibold text-semantic-error"
      title={reason || 'Structural check failed'}
    >
      Structurally failed
    </span>
  )
}

function RetryBadge({ attempt, max }: { attempt?: number; max?: number }) {
  if (!attempt) return null
  return (
    <span
      className="shrink-0 rounded-sm border border-amber-500/40 bg-amber-500/10 px-2 py-0.5 text-caption font-semibold text-amber-400"
      title="A transient failure was detected — re-running this step before committing the result."
    >
      ↻ Retrying {attempt}/{max ?? attempt + 1}
    </span>
  )
}

export function ProtocolBadge({ protocol }: { protocol?: string }) {
  if (!protocol) return null
  const p = PROTOCOL_BADGE[protocol.toLowerCase()]
  if (!p) return null
  return (
    <span className={cn('shrink-0 rounded-sm border px-2 py-0.5 text-caption font-semibold uppercase tracking-wide', p.className)}>
      {p.label}
    </span>
  )
}

function phaseBadgeClass(phase: ToolInvocationPhase): string {
  if (phase === 'running') {
    return 'bg-brand-primary-dim text-brand-primary-light border-blue-500/30'
  }
  if (phase === 'error') {
    return 'bg-semantic-errorDim text-semantic-error border-semantic-error/30'
  }
  return 'bg-semantic-successDim text-semantic-success border-semantic-success/30'
}

const preScrollableClass =
  'max-h-40 min-h-0 overflow-auto overscroll-contain whitespace-pre-wrap break-words rounded-sm bg-surface-base p-2 font-mono text-caption'

export function ToolRunCard({ trace }: ToolRunCardProps) {
  const wasRunning = useRef(trace.phase === 'running')
  // Start closed — only open on click for detail
  const [open, setOpen] = useState(false)
  const collapseTimer = useRef<ReturnType<typeof setTimeout> | null>(null)

  useEffect(() => {
    if (wasRunning.current && trace.phase !== 'running') {
      if (collapseTimer.current) clearTimeout(collapseTimer.current)
    }
    wasRunning.current = trace.phase === 'running'
    return () => {
      if (collapseTimer.current) clearTimeout(collapseTimer.current)
    }
  }, [trace.phase])

  const statusLabel = phaseLabel(trace.phase)
  const statusClass = phaseBadgeClass(trace.phase)

  return (
    <div className="rounded-md border border-surface-border bg-surface-base text-body-md">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        aria-label={`${trace.tool_name} invocation, ${statusLabel}. Toggle details.`}
        className="flex w-full items-center gap-2 px-3 py-1.5 text-left hover:bg-surface-overlay rounded-md transition-colors"
      >
        <span
          className={cn(
            'shrink-0 inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-[11px] font-semibold uppercase tracking-wide',
            statusClass,
          )}
        >
          {trace.phase === 'running' && (
            <svg
              className="animate-spin"
              width="9"
              height="9"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="3"
              strokeLinecap="round"
              aria-hidden
            >
              <path d="M21 12a9 9 0 1 1-6.219-8.56" />
            </svg>
          )}
          {statusLabel}
        </span>
        <ProtocolBadge protocol={trace.protocol} />
        {trace.phase === 'running' && trace.retryAttempt ? (
          <RetryBadge attempt={trace.retryAttempt} max={trace.maxAttempts} />
        ) : null}
        {trace.phase !== 'running' && (
          <VerifiedBadge verified={trace.verified} reason={trace.verdict_reason} />
        )}
        <span className="min-w-0 flex-1 truncate text-[12px] text-text-secondary">
          {trace.agent_id || trace.tool_name}
        </span>
        <span className="ml-auto shrink-0 text-[10px] text-text-disabled" aria-hidden>
          {open ? '▲' : '▼'}
        </span>
      </button>
      {open && (
        <div className="border-t border-surface-border px-3 py-2 space-y-2">
          {trace.total_cost_usd && trace.total_cost_usd !== '0' && (
            <div className="flex items-center gap-3 text-caption text-text-secondary">
              <span>
                <span className="font-medium text-text-body">Total cost:</span>{' '}
                <span className="font-mono text-amber-400">${parseFloat(trace.total_cost_usd).toFixed(6)}</span>
              </span>
              {trace.base_fee && trace.base_fee !== '0' && (
                <span className="text-text-secondary/60">
                  (agent fee: ${parseFloat(trace.base_fee).toFixed(6)})
                </span>
              )}
            </div>
          )}
          {trace.progressLines.length > 0 && (
            <ul className="list-disc space-y-1 pl-4 text-caption text-text-secondary">
              {trace.progressLines.map((line, i) => (
                <li key={`${trace.call_id}-p-${i}`}>{line}</li>
              ))}
            </ul>
          )}
          {Object.keys(trace.inputs).length > 0 && (
            <div className="min-h-0">
              <p className="mb-1 text-caption font-medium text-text-secondary">Query</p>
              <pre className={cn(preScrollableClass, 'text-text-secondary')}>
                {JSON.stringify(trace.inputs, null, 2)}
              </pre>
            </div>
          )}
          {trace.content_preview ? (
            <div className="min-h-0">
              <p className="mb-1 text-caption font-medium text-text-secondary">Result</p>
              <pre className={cn(preScrollableClass, 'text-text-body')}>
                {trace.content_preview}
              </pre>
            </div>
          ) : null}
        </div>
      )}
    </div>
  )
}
