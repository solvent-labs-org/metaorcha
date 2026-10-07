import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { sessions } from '../../api/client'
import { useSessionStore } from '../../store/session'
import { downloadRunAudit } from '../../lib/downloadAudit'
import { ProtocolBadge } from '../chat/ToolRunCard'
import { cn } from '../ui/cn'
import type { TranscriptEntryDTO } from '../../types/transcript'

interface AuditStep {
  internal_tool_name?: string
  /** Structural check (pipeline `_structural_verify`) — not a gate check. */
  verified?: boolean
  verdict_reason?: string
}

/** Latest settle-gate outcome for the run; absent when no gate evaluated it. */
interface AuditGate {
  outcome: string
  failed_checks?: string[]
  envelope_digest?: string
}

/** The one place a check name is turned into a label — see frontend/TRUST-INDICATORS.md. */
function GateIndicator({ gate }: { gate: AuditGate | null }) {
  if (!gate) return null
  if (gate.outcome === 'settled') {
    return (
      <span
        className="font-mono text-[10px] font-semibold text-semantic-success"
        title="settle gate: every check passed (schema, steps_root, steps_merkle_root, signature)"
      >
        settled
      </span>
    )
  }
  const checks = gate.failed_checks ?? []
  const named = checks[0] ?? 'unknown'
  return (
    <span
      className="font-mono text-[10px] font-semibold text-semantic-error"
      title={`settle gate refused: ${checks.join(', ') || 'no check named'}`}
    >
      refused — {named}
    </span>
  )
}

/** Per-step run inspector for the active session (developer mode). */
export function RunTab() {
  const sessionId = useSessionStore((s) => s.sessionId)
  const [auditError, setAuditError] = useState(false)

  const { data: transcript } = useQuery({
    queryKey: ['transcript', sessionId],
    queryFn: () => sessions.getTranscript(sessionId!),
    enabled: Boolean(sessionId),
    staleTime: 15_000,
  })

  // Audit is only used to enrich verdicts — silent when unavailable.
  const { data: audit } = useQuery({
    queryKey: ['run-audit', sessionId],
    queryFn: () => sessions.getAudit(sessionId!),
    enabled: Boolean(sessionId),
    retry: false,
  })

  const verdictByTool = useMemo(() => {
    const map = new Map<string, AuditStep>()
    const steps = (audit as { steps?: AuditStep[] } | undefined)?.steps
    if (Array.isArray(steps)) {
      for (const s of steps) {
        if (s.internal_tool_name) map.set(s.internal_tool_name, s)
      }
    }
    return map
  }, [audit])

  const gate = (audit as { gate?: AuditGate | null } | undefined)?.gate ?? null

  const steps = useMemo(() => {
    const entries = transcript?.entries ?? []
    return entries
      .map((e, i) => ({ entry: e, next: entries[i + 1] }))
      .filter(({ entry }) => entry.role === 'TOOL')
  }, [transcript])

  const handleDownloadAudit = async () => {
    if (!sessionId) return
    setAuditError(false)
    try {
      await downloadRunAudit(sessionId)
    } catch {
      setAuditError(true)
    }
  }

  if (!sessionId) {
    return <p className="px-4 font-mono text-caption text-text-disabled">No active session</p>
  }

  return (
    <div className="px-3">
      <div className="flex items-center justify-between mb-2 px-1">
        <p className="text-[10px] font-semibold text-text-disabled tracking-caps uppercase">
          Run Inspector
        </p>
        <GateIndicator gate={gate} />
        <button
          onClick={handleDownloadAudit}
          className="h-6 px-2 rounded-sm border border-surface-borderLight bg-surface-overlay font-mono text-[10px] text-text-body hover:border-surface-muted transition-colors"
        >
          download audit
        </button>
      </div>
      {auditError && (
        <p className="px-1 mb-2 font-mono text-[10px] text-semantic-error">
          audit download failed
        </p>
      )}

      {steps.length === 0 ? (
        <p className="px-1 font-mono text-caption text-text-disabled">
          No steps recorded for this session.
        </p>
      ) : (
        <div className="flex flex-col gap-1.5">
          {steps.map(({ entry, next }) => (
            <RunStepRow
              key={entry.tool_call_id ?? `seq-${entry.sequence_num}`}
              entry={entry}
              next={next}
              verdict={verdictByTool.get(entry.tool_name ?? '')}
            />
          ))}
        </div>
      )}
    </div>
  )
}

function durationMs(entry: TranscriptEntryDTO, next: TranscriptEntryDTO | undefined): number | null {
  if (!next) return null
  const a = Date.parse(entry.created_at)
  const b = Date.parse(next.created_at)
  if (Number.isNaN(a) || Number.isNaN(b)) return null
  const delta = b - a
  return delta > 0 && delta < 10 * 60_000 ? delta : null
}

function RunStepRow({
  entry,
  next,
  verdict,
}: {
  entry: TranscriptEntryDTO
  next: TranscriptEntryDTO | undefined
  verdict: AuditStep | undefined
}) {
  const [open, setOpen] = useState(false)

  const meta = (entry.tool_inputs ?? {}) as Record<string, unknown>
  const protocol = typeof meta.protocol === 'string' ? meta.protocol : undefined
  const agentId = typeof meta.agent_id === 'string' ? meta.agent_id : null
  const name = entry.tool_name ?? 'tool'

  const failed = entry.tool_status === 'error' || verdict?.verified === false
  // A step that merely succeeded is not "verified" — only the structural
  // check is a check, and it gates nothing (FR-14; frontend/TRUST-INDICATORS.md).
  const structurallyChecked = !failed && verdict?.verified === true
  const ms = durationMs(entry, next)

  return (
    <div className="rounded-md border border-surface-border bg-surface-overlay">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        aria-label={`${name} step details`}
        className="flex w-full items-center gap-1.5 px-2.5 py-2 text-left hover:bg-surface-muted/10 rounded-md transition-colors"
      >
        <span
          className={cn(
            'shrink-0 size-1.5 rounded-full',
            failed ? 'bg-semantic-error' : structurallyChecked ? 'bg-blue-400' : 'bg-surface-muted',
          )}
          aria-hidden
        />
        <span className="min-w-0 flex-1 truncate font-mono text-[11px] text-text-body">
          {agentId ?? name}
        </span>
        <ProtocolBadge protocol={protocol} />
        {failed ? (
          <span
            className="shrink-0 font-mono text-[10px] font-semibold text-semantic-error"
            title={verdict?.verdict_reason ?? 'step failed'}
          >
            failed
          </span>
        ) : structurallyChecked ? (
          <span
            className="shrink-0 font-mono text-[10px] font-semibold text-blue-400"
            title={`structural check passed — not a gate check (${verdict?.verdict_reason ?? 'ok'})`}
          >
            structurally checked
          </span>
        ) : null}
        {ms !== null && (
          <span className="shrink-0 font-mono text-[10px] text-text-disabled">
            {(ms / 1000).toFixed(1)}s
          </span>
        )}
        <span className="shrink-0 text-[10px] text-text-disabled" aria-hidden>
          {open ? '▲' : '▼'}
        </span>
      </button>
      {open && (
        <pre className="border-t border-surface-border max-h-48 overflow-auto overscroll-contain px-2.5 py-2 font-mono text-[10px] text-text-secondary whitespace-pre-wrap break-words">
          {JSON.stringify(entry, null, 2)}
        </pre>
      )}
    </div>
  )
}
