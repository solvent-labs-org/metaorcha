/**
 * HitlApprovalModal — shown for HITL_APPROVAL interrupts.
 *
 * When the interrupt metadata contains `drafts` (email review), each draft is
 * rendered as an editable card so the user can review and modify subject/body
 * before approving. Edited drafts are sent back with the resume payload so the
 * downstream agent sends the correct content.
 *
 * Without drafts, renders a simple approve/deny confirmation.
 */
import { useState } from 'react'
import type { HitlApprovalMetadata, Interrupt } from '../../types'
import { sessions } from '../../api/client'
import { Button } from '../ui/Button'
import { cn } from '../ui/cn'

interface EmailDraft {
  to: string
  subject: string
  body: string
  first_name?: string
  company?: string
  [key: string]: unknown
}

interface Props {
  interrupt: Interrupt
  sessionId: string
  streamResponse: (res: Response) => Promise<void>
  onAfterStream: () => Promise<void>
  onDismiss: () => void
}

const RISK_STYLES: Record<string, { label: string; pill: string }> = {
  low: {
    label: 'Low Risk',
    pill: 'bg-semantic-successDim text-semantic-success border border-[rgba(34,197,94,0.3)]',
  },
  medium: {
    label: 'Medium Risk',
    pill: 'bg-semantic-warningDim text-semantic-warning border border-[rgba(245,158,11,0.3)]',
  },
  high: {
    label: 'High Risk',
    pill: 'bg-semantic-errorDim text-semantic-error border border-[rgba(239,68,68,0.3)]',
  },
}

export function HitlApprovalModal({
  interrupt,
  sessionId,
  streamResponse,
  onAfterStream,
  onDismiss,
}: Props) {
  const meta = interrupt.metadata as Partial<HitlApprovalMetadata> & {
    drafts?: EmailDraft[]
    task_id?: string
    endpoint?: string
  }

  const rawDrafts: EmailDraft[] = meta.drafts ?? []
  const [drafts, setDrafts] = useState<EmailDraft[]>(rawDrafts)
  const [expandedIdx, setExpandedIdx] = useState<number | null>(rawDrafts.length > 0 ? 0 : null)
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const actionDescription = meta.action_description ?? interrupt.message
  const riskLevel = (meta.risk_level ?? 'medium') as 'low' | 'medium' | 'high'
  const agentName = meta.agent_display_name ?? null
  const capabilityName = meta.capability_name ?? null
  const riskStyle = RISK_STYLES[riskLevel] ?? RISK_STYLES.medium
  const hasDrafts = drafts.length > 0
  // Scope gate (story 1.5): the card names the connection, the capability
  // and the target; a destructive call says so in as many words.
  const scopeClass = meta.scope_class ?? null
  const connectionName = meta.connection_name ?? null
  const target = meta.target ?? null
  const isScopeGate = scopeClass !== null

  const updateDraft = (idx: number, field: 'subject' | 'body', value: string) => {
    setDrafts((prev) => prev.map((d, i) => (i === idx ? { ...d, [field]: value } : d)))
  }

  const resume = async (approved: boolean) => {
    setSubmitting(true)
    setError(null)
    try {
      const payload: Record<string, unknown> = {
        status: approved ? 'approved' : 'denied',
      }
      if (approved && hasDrafts) {
        payload.edited_drafts = drafts
      }
      const res = await sessions.resume(
        sessionId,
        interrupt.interrupt_id,
        interrupt.interrupt_type,
        payload,
      )
      if (res.ok) {
        await streamResponse(res)
        await onAfterStream()
      } else {
        setError(`Server error (${res.status})`)
        setSubmitting(false)
      }
    } catch {
      setError('Could not reach the server — check your connection')
      setSubmitting(false)
    }
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center"
      role="dialog"
      aria-modal="true"
      aria-label="Approval Required"
    >
      {/* Backdrop */}
      <div
        className="absolute inset-0 bg-surface-canvas/70"
        onClick={() => !submitting && onDismiss()}
        aria-hidden="true"
      />

      <div
        className={cn(
          'relative flex flex-col bg-surface-elevated border border-surface-border rounded-lg shadow-lg overflow-hidden',
          hasDrafts ? 'w-[640px] max-h-[85vh]' : 'w-[460px]',
        )}
      >
        {/* Header */}
        <div className="flex items-center gap-3 px-5 h-14 border-b border-surface-border shrink-0">
          <div className="size-9 flex items-center justify-center rounded-md bg-semantic-warningDim text-base">
            ⚠️
          </div>
          <div className="flex-1 min-w-0">
            <p className="text-[15px] font-semibold text-text-heading leading-none">
              {hasDrafts ? 'Review Outreach Emails' : 'Approval Required'}
            </p>
            {agentName && (
              <p className="text-caption text-text-secondary mt-0.5">{agentName}</p>
            )}
          </div>
          <span className={`text-[11px] font-medium px-2 py-0.5 rounded-full ${riskStyle.pill}`}>
            {riskStyle.label}
          </span>
        </div>

        {/* Body */}
        <div className="flex-1 overflow-y-auto scrollbar-thin">
          <div className="p-5 flex flex-col gap-3">
            <p className="text-body-md text-text-body">{actionDescription}</p>
            {capabilityName && !hasDrafts && (
              <p className="text-caption text-text-secondary">
                <span className="font-medium">Capability:</span> {capabilityName}
              </p>
            )}
            {isScopeGate && (
              <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-caption text-text-secondary">
                {connectionName && (
                  <>
                    <dt className="font-medium">Connection</dt>
                    <dd className="truncate">{connectionName}</dd>
                  </>
                )}
                {target && (
                  <>
                    <dt className="font-medium">Target</dt>
                    <dd className="break-all font-mono">{target}</dd>
                  </>
                )}
                <dt className="font-medium">Action</dt>
                <dd>
                  {scopeClass === 'destructive'
                    ? 'Destructive — deletes or changes something that cannot be undone from here'
                    : 'Write — changes something on your platform'}
                </dd>
              </dl>
            )}
            {isScopeGate && (
              <p className="text-caption text-text-secondary">
                Your decision is recorded in the run receipt under your identity.
              </p>
            )}

            {/* Email draft cards */}
            {hasDrafts && (
              <div className="flex flex-col gap-2 mt-1">
                <p className="text-caption text-text-secondary">
                  Review and optionally edit each email before sending. Changes are saved automatically.
                </p>
                {drafts.map((draft, idx) => (
                  <div
                    key={idx}
                    className="rounded-md border border-surface-border bg-surface-base overflow-hidden"
                  >
                    {/* Draft header */}
                    <button
                      type="button"
                      onClick={() => setExpandedIdx(expandedIdx === idx ? null : idx)}
                      className="w-full flex items-center gap-2 px-4 py-2.5 text-left hover:bg-surface-overlay transition-colors"
                    >
                      <span className="text-[11px] font-semibold text-text-secondary shrink-0">
                        #{idx + 1}
                      </span>
                      <span className="text-label text-text-body truncate flex-1">
                        {draft.to || draft.first_name || `Email ${idx + 1}`}
                        {draft.company ? ` · ${draft.company}` : ''}
                      </span>
                      <span className="text-[11px] text-text-disabled shrink-0">
                        {expandedIdx === idx ? '▲' : '▼'}
                      </span>
                    </button>

                    {expandedIdx === idx && (
                      <div className="px-4 pb-4 flex flex-col gap-2 border-t border-surface-border">
                        <div className="flex flex-col gap-1 mt-3">
                          <label className="text-caption font-medium text-text-secondary">To</label>
                          <p className="text-label text-text-body">{draft.to || '—'}</p>
                        </div>
                        <div className="flex flex-col gap-1">
                          <label className="text-caption font-medium text-text-secondary">Subject</label>
                          <input
                            value={draft.subject ?? ''}
                            onChange={(e) => updateDraft(idx, 'subject', e.target.value)}
                            className="h-8 px-3 rounded-md bg-surface-canvas border border-surface-borderLight text-label text-text-body focus:outline-none focus:border-brand-primary"
                          />
                        </div>
                        <div className="flex flex-col gap-1">
                          <label className="text-caption font-medium text-text-secondary">Body</label>
                          <textarea
                            value={draft.body ?? ''}
                            onChange={(e) => updateDraft(idx, 'body', e.target.value)}
                            rows={5}
                            className="px-3 py-2 rounded-md bg-surface-canvas border border-surface-borderLight text-label text-text-body focus:outline-none focus:border-brand-primary resize-none"
                          />
                        </div>
                      </div>
                    )}
                  </div>
                ))}
              </div>
            )}

            {error && (
              <p className="text-caption text-semantic-error">{error}</p>
            )}
          </div>
        </div>

        {/* Footer */}
        <div className="flex items-center gap-2 px-5 py-4 border-t border-surface-border shrink-0">
          <Button
            size="sm"
            onClick={() => resume(false)}
            disabled={submitting}
            className="flex-1 bg-surface-overlay border border-surface-borderLight text-text-body hover:border-surface-muted"
          >
            {hasDrafts ? 'Cancel' : 'Deny'}
          </Button>
          <Button
            size="sm"
            onClick={() => resume(true)}
            loading={submitting}
            className="flex-1 bg-semantic-success hover:bg-semantic-success/90 text-white border-0"
          >
            {hasDrafts ? `Send ${drafts.length} Email${drafts.length !== 1 ? 's' : ''}` : 'Approve'}
          </Button>
        </div>
      </div>
    </div>
  )
}
