import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useParams } from 'react-router-dom'
import { Sidebar } from '../components/layout/Sidebar'
import { SessionListPanel } from '../components/layout/SessionListPanel'
import { RightPanel } from '../components/layout/RightPanel'
import { ToolTimelineRow } from '../components/chat/ExecutionTimeline'
import { DownloadReceiptButton } from '../components/chat/DownloadReceiptButton'
import { MessageBubble } from '../components/chat/MessageBubble'
import { StreamingDots } from '../components/chat/StreamingDots'
import { ModelChip } from '../components/chat/ModelChip'
import { InputBar } from '../components/ui/InputBar'
import { CredentialsModal } from '../components/modals/CredentialsModal'
import { SaveWorkflowModal } from '../components/modals/SaveWorkflowModal'
import { CrmSetupModal } from '../components/modals/CrmSetupModal'
import { IntegrationsModal } from '../components/modals/IntegrationsModal'
import { useSessionStore } from '../store/session'
import { useSessionSidebarStore } from '../store/sessionSidebar'
import { useAuthStore } from '../store/auth'
import { useSettingsStore } from '../store/settings'
import { useByokStore } from '../store/byok'
import { sessions, files } from '../api/client'
import { useSSE } from '../hooks/useSSE'
import { useSessionStatusSync } from '../hooks/useSessionStatusSync'
import { cn } from '../components/ui/cn'
import { buildChatTimeline } from '../lib/buildChatTimeline'
import { downloadRunAudit } from '../lib/downloadAudit'
import { CanvasRenderer } from '../components/canvas'
import { queryClient } from '../lib/queryClient'
import type {
  AgentClarificationMetadata,
  AttachedArtifactRef,
  HitlClarificationMetadata,
  Interrupt,
  InsufficientCreditsMetadata,
  PendingArtifact,
} from '../types'
import { HitlApprovalModal } from '../components/modals/HitlApprovalModal'
import { OAuthPopupHandler } from '../components/modals/OAuthPopupHandler'
import { InterruptCredentialModal } from '../components/modals/InterruptCredentialModal'
import { AgentInterruptModal } from '../components/interrupts/AgentInterruptModal'
import { leadGenBaseUrl } from '../lib/leadGenBaseUrl'

const STATUS_LABELS: Record<string, { label: string; color: string }> = {
  idle: { label: '● ready', color: 'text-text-secondary' },
  running: { label: '● running', color: 'text-brand-primary-light' },
  interrupted: { label: '● interrupted', color: 'text-semantic-warning' },
  complete: { label: '● complete', color: 'text-semantic-success' },
  failed: { label: '● failed', color: 'text-semantic-error' },
}

const SUGGESTED_GOALS = [
  'Show me my portfolio performance',
  'Show me my portfolio performance, use your web scraper agent to summarize https://en.wikipedia.org/wiki/Nvidia, and screenshot the Alpaca dashboard',
  'Summarize https://en.wikipedia.org/wiki/Artificial_intelligence with the web scraper agent',
]

export function Chat() {
  const { sessionId: urlSessionId } = useParams<{ sessionId: string }>()
  const [input, setInput] = useState('')
  const [pendingArtifacts, setPendingArtifacts] = useState<PendingArtifact[]>([])
  const [receiptOpen, setReceiptOpen] = useState(false)
  const [receiptEmail, setReceiptEmail] = useState('')
  const [receiptError, setReceiptError] = useState<string | null>(null)
  const bottomRef = useRef<HTMLDivElement>(null)
  const isAuthenticated = useAuthStore((s) => s.isAuthenticated)
  const sessionSidebarOpen = useSessionSidebarStore((s) => s.isOpen)
  const defaultModel = useSettingsStore((s) => s.defaultModel)
  const byokMode = useByokStore((s) => s.mode)

  const store = useSessionStore()
  const { streamResponse, abort } = useSSE()
  const { refetchSessionDetails } = useSessionStatusSync()

  // Push BYOK '__llm__' credentials onto the active session (no-op when hosted
  // or already applied to this session).
  useEffect(() => {
    if (!store.sessionId) return
    if (useSessionStore.getState().byokSessionId === store.sessionId) return
    void useByokStore.getState().applyToSession(store.sessionId)
  }, [store.sessionId, byokMode])

  const chatTimeline = useMemo(
    () => buildChatTimeline(store.messages, store.toolTrace, store.canvases),
    [store.messages, store.toolTrace, store.canvases],
  )

  const { data: transcriptData } = useQuery({
    queryKey: ['transcript', urlSessionId],
    queryFn: () => sessions.getTranscript(urlSessionId!),
    enabled: Boolean(urlSessionId && isAuthenticated),
    staleTime: 15_000,
  })

  useEffect(() => {
    if (!urlSessionId) return
    const st = useSessionStore.getState()
    if (st.sessionId === urlSessionId) return
    st.reset()
    st.setSessionId(urlSessionId)
  }, [urlSessionId])

  useEffect(() => {
    if (!urlSessionId || !transcriptData?.entries?.length) return
    const s = useSessionStore.getState()
    if (s.sessionId !== urlSessionId) return
    if (s.streamingMessageId || s.status === 'running') return
    s.hydrateFromTranscript(urlSessionId, transcriptData.entries)
  }, [urlSessionId, transcriptData])

  // Auto-scroll to bottom on new messages
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [
    chatTimeline.length,
    store.streamingMessageId,
    store.messages.length,
    store.toolTrace.length,
  ])

  /** Shared post-resume / post-stream callback used by all interrupt handlers. */
  const onAfterInterrupt = useCallback(async () => {
    await refetchSessionDetails()
    void queryClient.invalidateQueries({ queryKey: ['transcript', store.sessionId] })
    void queryClient.invalidateQueries({ queryKey: ['sessions'] })
  }, [refetchSessionDetails, store.sessionId])

  const handleFileSelected = async (file: File) => {
    if (!store.sessionId) return
    const tempId = crypto.randomUUID()
    const placeholder: PendingArtifact = {
      artifact_id: tempId,
      filename: file.name,
      mime_type: file.type,
      size_bytes: file.size,
      session_id: store.sessionId,
      uploading: true,
    }
    setPendingArtifacts((prev) => [...prev, placeholder])
    try {
      const result = await files.upload(file, store.sessionId)
      setPendingArtifacts((prev) =>
        prev.map((a) =>
          a.artifact_id === tempId ? { ...result, uploading: false } : a,
        ),
      )
    } catch (err) {
      const msg = err instanceof Error ? err.message : 'Upload failed'
      setPendingArtifacts((prev) =>
        prev.map((a) =>
          a.artifact_id === tempId ? { ...a, uploading: false, error: msg } : a,
        ),
      )
    }
  }

  const handleRemoveArtifact = (artifactId: string) => {
    setPendingArtifacts((prev) => prev.filter((a) => a.artifact_id !== artifactId))
  }

  const handleSend = async (message: string, artifactIds: string[]) => {
    if (!store.sessionId) return
    store.clearToolTrace()
    const content =
      message ||
      (artifactIds.length > 0
        ? `[Attached ${artifactIds.length} file${artifactIds.length > 1 ? 's' : ''}]`
        : '')
    const attachedArtifacts: AttachedArtifactRef[] = pendingArtifacts
      .filter(
        (a) =>
          artifactIds.includes(a.artifact_id) && !a.uploading && !a.error,
      )
      .map((a) => ({
        artifact_id: a.artifact_id,
        filename: a.filename,
        mime_type: a.mime_type,
        size_bytes: a.size_bytes,
      }))
    store.addMessage({
      id: crypto.randomUUID(),
      role: 'user',
      content,
      timestamp: Date.now(),
      attachedArtifacts:
        attachedArtifacts.length > 0 ? attachedArtifacts : undefined,
    })
    setInput('')
    setPendingArtifacts([])
    try {
      const byok = useByokStore.getState()
      const res = await sessions.sendMessage(store.sessionId, message, artifactIds, {
        model:
          byok.mode === 'byok' && byok.model.trim()
            ? byok.model.trim()
            : defaultModel,
        customInstructions: byok.systemPrompt.trim() || undefined,
      })
      if (res.ok) {
        await streamResponse(res)
        await onAfterInterrupt()
      } else {
        store.addMessage({
          id: crypto.randomUUID(),
          role: 'error',
          content: `Request failed (${res.status})`,
          timestamp: Date.now(),
        })
        store.setStatus('failed')
      }
    } catch {
      store.addMessage({
        id: crypto.randomUUID(),
        role: 'error',
        content: 'Could not reach the server — check your connection',
        timestamp: Date.now(),
      })
      store.setStatus('failed')
    }
  }

  const handleStop = async () => {
    if (!store.sessionId || store.status !== 'running') return
    abort()
    try {
      await sessions.stop(store.sessionId)
      store.appendSessionLog('Stop requested')
      store.setStatus('idle')
    } catch {
      store.addMessage({
        id: crypto.randomUUID(),
        role: 'error',
        content: 'Could not stop the current execution',
        timestamp: Date.now(),
      })
      store.setStatus('failed')
    }
  }

  const handleDownloadAudit = async () => {
    if (!store.sessionId) return
    try {
      await downloadRunAudit(store.sessionId)
    } catch {
      store.addMessage({
        id: crypto.randomUUID(),
        role: 'error',
        content: 'Could not download the run audit',
        timestamp: Date.now(),
      })
    }
  }

  const handleReceiptSubmit = () => {
    const email = receiptEmail.trim()
    if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) {
      setReceiptError('Enter a valid email address')
      return
    }
    setReceiptOpen(false)
    setReceiptEmail('')
    setReceiptError(null)
    void handleSend(`email the run receipt for this session to ${email}`, [])
  }

  const statusInfo = STATUS_LABELS[store.status] ?? STATUS_LABELS.idle
  const sessionTitle = store.messages[0]?.content?.slice(0, 48) ?? 'New Session'

  return (
    <div className="flex h-screen bg-surface-canvas overflow-hidden">
      <Sidebar />
      {isAuthenticated ? <SessionListPanel /> : null}

      <div
        className={cn(
          'flex min-w-0 flex-1 flex-col transition-[margin-left] duration-200 ease-out motion-reduce:transition-none',
          isAuthenticated && sessionSidebarOpen && 'ml-80',
          !isAuthenticated || !sessionSidebarOpen ? 'ml-16' : null,
        )}
      >
        {/* Chat header */}
        <header
          className={cn(
            'flex h-14 shrink-0 items-center border-b border-surface-border bg-surface-base px-5',
            isAuthenticated && !sessionSidebarOpen && 'pl-14',
          )}
        >
          <h1 className="text-[15px] font-semibold text-text-heading truncate flex-1 max-w-[400px]">
            {sessionTitle}
          </h1>
          <div
            className="ml-3 flex items-center h-6 px-2.5 rounded-sm bg-brand-primary-dim border border-[var(--accent-border)] shrink-0"
          >
            <span className={cn('text-[11px] font-medium', statusInfo.color)}>
              {statusInfo.label}
            </span>
          </div>
          {store.byokSessionId && store.byokSessionId === store.sessionId && (
            <div className="ml-2 flex items-center h-6 px-2.5 rounded-sm bg-surface-overlay border border-surface-border shrink-0">
              <span className="font-mono text-[10px] text-text-secondary">
                running on your model
              </span>
            </div>
          )}
          <div className="flex-1" />
          <div className="flex items-center gap-2">
            <span className="font-mono text-[10px] text-text-disabled select-none">beta</span>
            <IntegrationsButton />
            <button
              onClick={handleDownloadAudit}
              disabled={store.messages.length === 0}
              aria-label="Download run audit"
              title="Download per-run audit (Verified Runs evidence)"
              className="flex items-center gap-1.5 h-8 px-3 rounded-md bg-surface-overlay border border-surface-borderLight text-[12px] font-medium text-text-body hover:border-surface-muted disabled:opacity-50 transition-colors"
            >
              ⬇ Audit
            </button>
            {store.status === 'complete' && (
              <button
                onClick={() => {
                  setReceiptOpen((open) => !open)
                  setReceiptError(null)
                }}
                aria-label="Email run receipt"
                title="Email the run receipt for this session"
                className="flex items-center gap-1.5 h-8 px-3 rounded-md border border-transparent text-[12px] font-medium text-text-secondary hover:text-text-body hover:border-surface-borderLight transition-colors"
              >
                ✉ receipt
              </button>
            )}
            <button
              onClick={store.openCredentialsModal}
              aria-label="Manage credentials"
              className="flex items-center gap-1.5 h-8 px-3 rounded-md bg-surface-overlay border border-surface-borderLight text-[12px] font-medium text-text-body hover:border-surface-muted transition-colors"
            >
              🔑 Credentials
            </button>
          </div>
        </header>

        {/* Receipt email inline prompt */}
        {receiptOpen && store.status === 'complete' && (
          <div className="flex items-center gap-2 px-5 py-2 border-b border-surface-border bg-surface-base shrink-0">
            <span className="font-mono text-[11px] text-text-secondary shrink-0">receipt →</span>
            <input
              value={receiptEmail}
              onChange={(e) => {
                setReceiptEmail(e.target.value)
                setReceiptError(null)
              }}
              onKeyDown={(e) => {
                if (e.key === 'Enter') handleReceiptSubmit()
                if (e.key === 'Escape') setReceiptOpen(false)
              }}
              placeholder="you@example.com"
              aria-label="Receipt email address"
              autoFocus
              className="w-56 h-7 px-2 rounded-md bg-surface-base border border-surface-borderLight font-mono text-[11px] text-text-body placeholder:text-text-disabled focus:outline-none focus:border-brand-primary"
            />
            <button
              onClick={handleReceiptSubmit}
              className="h-7 px-2.5 rounded-md bg-surface-overlay border border-surface-borderLight text-[11px] font-medium text-text-body hover:border-surface-muted transition-colors"
            >
              send
            </button>
            <button
              onClick={() => setReceiptOpen(false)}
              aria-label="Close receipt prompt"
              className="text-[11px] text-text-disabled hover:text-text-secondary transition-colors"
            >
              ✕
            </button>
            {receiptError && (
              <span className="text-[11px] text-semantic-error">{receiptError}</span>
            )}
          </div>
        )}

        {/* Messages */}
        <div className="flex-1 overflow-y-auto scrollbar-thin px-5 py-5">
          <div className="mx-auto flex w-full max-w-3xl flex-col gap-3">
            <ul
              className="m-0 flex list-none flex-col gap-3 p-0"
              aria-label="Conversation and execution timeline"
            >
              {chatTimeline.map((item) =>
                item.kind === 'message' ? (
                  // Hide all agent messages when a canvas exists — the dashboard
                  // is the output; text summaries are noise.
                  item.msg.role === 'agent' && store.canvases.length > 0 ? null :
                  // Also hide streaming/thinking messages — they're internal reasoning.
                  item.msg.role === 'agent' && (item.msg.streaming || item.msg.streamedAsThinking) ? null :
                  // Receipt-only markers have no text; the download control is below.
                  item.msg.role === 'agent' && item.msg.content.trim() === '' ? null :
                  item.msg.role === 'user' ? (
                    <li key={item.msg.id} className="list-none animate-message-in">
                      <MessageBubble message={item.msg} />
                    </li>
                  ) : (
                    <li key={item.msg.id} className="list-none animate-message-in">
                      <MessageBubble message={item.msg} />
                    </li>
                  )
                ) : item.kind === 'canvas' ? (
                  <li key={item.entry.id} className="list-none">
                    <CanvasRenderer manifest={item.entry.manifest} />
                  </li>
                ) : (
                  <ToolTimelineRow key={item.trace.call_id} trace={item.trace} />
                ),
              )}
              {store.messages
                .filter((m): m is typeof m & { runId: string } => Boolean(m.runId))
                .map((m) => (
                  <li key={`receipt-${m.runId}`} className="list-none">
                    <DownloadReceiptButton runId={m.runId} />
                  </li>
                ))}
              {store.status === 'running' && !store.streamingMessageId && (
                <li className="list-none">
                  <StreamingDots phase={store.runPhase} agentName={store.activeAgentName} />
                </li>
              )}
            </ul>

            {/* Interrupt cards — dispatched by interrupt_type */}
            {store.interrupts.map((interrupt) => (
              <InterruptDispatch
                key={interrupt.interrupt_id}
                interrupt={interrupt}
                sessionId={store.sessionId ?? ''}
                streamResponse={streamResponse}
                onAfterInterrupt={onAfterInterrupt}
              />
            ))}

            {/* Run telemetry strip — after a run completes */}
            {store.status === 'complete' && store.sessionId && (
              <RunTelemetryStrip sessionId={store.sessionId} />
            )}

            <div ref={bottomRef} />
          </div>
        </div>

        {/* Input bar */}
        <div className="px-5 py-4 border-t border-surface-border shrink-0">
          <div className="mx-auto w-full max-w-3xl">
          {store.messages.length === 0 && store.status !== 'running' && (
            <div className="flex flex-wrap items-center gap-2 mb-3">
              <span className="text-[12px] text-text-secondary">Try:</span>
              {SUGGESTED_GOALS.map((goal) => (
                <button
                  key={goal}
                  onClick={() => void handleSend(goal, [])}
                  className="h-8 max-w-full truncate px-3 rounded-full bg-surface-overlay border border-surface-borderLight text-[12px] font-medium text-text-body hover:border-brand-primary transition-colors"
                >
                  {goal}
                </button>
              ))}
            </div>
          )}
          {store.messages.length === 0 && store.status !== 'running' && (
            <p className="mb-3 font-mono text-[11px] text-text-disabled">
              works today: portfolio · web summaries · screenshots · email me the receipt
            </p>
          )}
          {store.status === 'failed' && (
            <div className="flex items-center gap-2 mb-2">
              <span className="text-[12px] text-semantic-error">Something went wrong.</span>
              <button
                onClick={() => store.setStatus('idle')}
                className="text-[12px] font-medium text-brand-primary-light hover:underline"
              >
                Retry
              </button>
            </div>
          )}
          <InputBar
            value={input}
            onChange={setInput}
            onSubmit={handleSend}
            onStop={handleStop}
            onFileSelected={handleFileSelected}
            pendingArtifacts={pendingArtifacts}
            onRemoveArtifact={handleRemoveArtifact}
            placeholder="Continue the conversation…"
            disabled={store.status === 'running'}
            isRunning={store.status === 'running'}
            size="chat"
          />
          <div className="mt-2 flex items-center">
            <ModelChip />
          </div>
          </div>
        </div>
      </div>

      {/* Right Panel */}
      <RightPanel />

      {/* Modals */}
      <CredentialsModal />
      <SaveWorkflowModal />
      <CrmSetupModal />
      <IntegrationsModal />
    </div>
  )
}

// ── Run telemetry strip ────────────────────────────────────────────────────────

interface RunAuditSummary {
  total_steps?: number
  protocols?: string[]
  total_cost_usd?: string
  duration_ms?: number | null
}

function formatRunTelemetry(audit: Record<string, unknown> | undefined): string | null {
  const summary = (audit?.summary ?? {}) as RunAuditSummary
  const parts: string[] = []
  if (typeof summary.total_steps === 'number') parts.push(`steps: ${summary.total_steps}`)
  if (Array.isArray(summary.protocols) && summary.protocols.length > 0) {
    parts.push(`protocols: ${summary.protocols.join('+')}`)
  }
  if (summary.total_cost_usd) parts.push(`cost: $${summary.total_cost_usd}`)
  if (typeof summary.duration_ms === 'number') {
    parts.push(`${(summary.duration_ms / 1000).toFixed(1)}s`)
  }
  return parts.length > 0 ? parts.join(' · ') : null
}

function RunTelemetryStrip({ sessionId }: { sessionId: string }) {
  // Silent on failure — no strip if the audit can't be fetched.
  const { data } = useQuery({
    queryKey: ['run-audit', sessionId],
    queryFn: () => sessions.getAudit(sessionId),
    retry: false,
  })
  const line = formatRunTelemetry(data)
  if (!line) return null
  return <p className="font-mono text-[11px] text-text-disabled">{line}</p>
}

// ── Interrupt dispatcher ───────────────────────────────────────────────────────

interface InterruptDispatchProps {
  interrupt: Interrupt
  sessionId: string
  streamResponse: (res: Response) => Promise<void>
  onAfterInterrupt: () => Promise<void>
}

function InterruptDispatch({
  interrupt,
  sessionId,
  streamResponse,
  onAfterInterrupt,
}: InterruptDispatchProps) {
  const noop = () => { /* dismiss is handled by stream completion */ }

  switch (interrupt.interrupt_type) {
    case 'AUTH_FORM_SUBMISSION':
      return (
        <InterruptCredentialModal
          interrupt={interrupt}
          sessionId={sessionId}
          streamResponse={streamResponse}
          onAfterStream={onAfterInterrupt}
          onDismiss={noop}
        />
      )

    case 'HITL_APPROVAL':
      return (
        <HitlApprovalModal
          interrupt={interrupt}
          sessionId={sessionId}
          streamResponse={streamResponse}
          onAfterStream={onAfterInterrupt}
          onDismiss={noop}
        />
      )

    case 'AUTH_CALLBACK':
    case 'AGENT_OAUTH_CALLBACK':
      return (
        <InlineInterruptCard interrupt={interrupt}>
          <OAuthPopupHandler
            interrupt={interrupt}
            sessionId={sessionId}
            onAfterComplete={onAfterInterrupt}
            onDeny={onAfterInterrupt}
            streamResponse={streamResponse}
          />
        </InlineInterruptCard>
      )

    case 'CRM_SETUP':
      return (
        <AgentInterruptModal
          interrupt={interrupt}
          sessionId={sessionId}
          streamResponse={streamResponse}
          onAfterStream={onAfterInterrupt}
        />
      )

    case 'INSUFFICIENT_CREDITS':
      return (
        <InsufficientCreditsModal interrupt={interrupt} />
      )

    case 'HITL_CLARIFICATION':
    case 'AGENT_CLARIFICATION':
    default:
      return (
        <InlineInterruptCard interrupt={interrupt}>
          <ClarificationInput
            interrupt={interrupt}
            sessionId={sessionId}
            streamResponse={streamResponse}
            onAfterStream={onAfterInterrupt}
          />
        </InlineInterruptCard>
      )
  }
}

// ── Shared inline card wrapper ─────────────────────────────────────────────────

function InlineInterruptCard({
  interrupt,
  children,
}: {
  interrupt: Interrupt
  children: React.ReactNode
}) {
  const title =
    interrupt.interrupt_type === 'AGENT_CLARIFICATION'
      ? 'Agent Input Required'
      : 'Input Required'

  return (
    <div className="mx-0 px-4 py-3 rounded-md bg-semantic-warningDim border border-[var(--warning-border)]">
      <p className="text-label font-medium text-semantic-warning mb-2">⚠ {title}</p>
      {children}
    </div>
  )
}

// ── Insufficient credits modal ─────────────────────────────────────────────────

function InsufficientCreditsModal({ interrupt }: { interrupt: Interrupt }) {
  const [dismissed, setDismissed] = useState(false)
  const meta = interrupt.metadata as Partial<InsufficientCreditsMetadata>
  const reason = meta.reason ?? 'insufficient_credits'
  const amountOwed = meta.amount_owed && meta.amount_owed !== '0' ? meta.amount_owed : null
  const agentName = meta.agent_display_name || 'the agent'

  if (dismissed) return null

  const title = reason === 'arrears' ? 'Account in Arrears' : 'Insufficient Credits'
  const body =
    reason === 'arrears'
      ? `Your account has an outstanding balance${amountOwed ? ` of $${amountOwed}` : ''}. Clear it to continue.`
      : `You don't have enough credits to invoke ${agentName}. Top up to resume.`

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center"
      role="dialog"
      aria-modal="true"
      aria-label={title}
    >
      <div
        className="absolute inset-0 bg-surface-canvas/70"
        onClick={() => setDismissed(true)}
        aria-hidden="true"
      />
      <div className="relative w-[420px] flex flex-col bg-surface-elevated border border-surface-border rounded-lg shadow-lg overflow-hidden">
        {/* Header */}
        <div className="flex items-center gap-3 px-5 h-14 border-b border-surface-border">
          <div className="size-9 flex items-center justify-center rounded-md bg-semantic-errorDim text-base">
            💳
          </div>
          <p className="flex-1 text-[15px] font-semibold text-text-heading">{title}</p>
          <button
            onClick={() => setDismissed(true)}
            aria-label="Close"
            className="size-7 flex items-center justify-center rounded-md text-text-secondary hover:bg-surface-overlay hover:text-text-body transition-colors"
          >
            ✕
          </button>
        </div>
        {/* Body */}
        <div className="p-5">
          <p className="text-body-md text-text-body">{body}</p>
        </div>
        {/* Footer */}
        <div className="flex items-center gap-2 px-5 py-4 border-t border-surface-border">
          <button
            onClick={() => setDismissed(true)}
            className="flex-1 h-9 rounded-md bg-surface-overlay border border-surface-borderLight text-label text-text-body hover:border-surface-muted transition-colors"
          >
            Close
          </button>
          <a
            href="/settings"
            onClick={() => setDismissed(true)}
            className="flex-1 h-9 flex items-center justify-center rounded-md bg-brand-primary text-white text-label font-medium hover:bg-brand-primary-hover transition-colors"
          >
            Top Up Credits
          </a>
        </div>
      </div>
    </div>
  )
}

// ── Clarification text input (HITL + Agent) ────────────────────────────────────

function ClarificationInput({
  interrupt,
  sessionId,
  streamResponse,
  onAfterStream,
}: {
  interrupt: Interrupt
  sessionId: string
  streamResponse: (res: Response) => Promise<void>
  onAfterStream: () => Promise<void>
}) {
  const meta = interrupt.metadata as Partial<HitlClarificationMetadata & AgentClarificationMetadata>
  const question = meta.question ?? interrupt.message

  const [value, setValue] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const handleSubmit = async () => {
    if (!value.trim()) return
    setSubmitting(true)
    setError(null)
    try {
      const res = await sessions.resume(
        sessionId,
        interrupt.interrupt_id,
        interrupt.interrupt_type,
        { status: 'complete', response: value.trim() },
      )
      if (res.ok) {
        await streamResponse(res)
        await onAfterStream()
      } else {
        setError(`Server error (${res.status})`)
      }
    } catch {
      setError('Could not reach the server — check your connection')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="flex flex-col gap-2">
      {question && question !== interrupt.message && (
        <p className="text-body-md text-text-body">{question}</p>
      )}
      {!question && (
        <p className="text-body-md text-text-body">{interrupt.message}</p>
      )}
      {error && <p className="text-caption text-semantic-error">{error}</p>}
      <div className="flex gap-2">
        <input
          value={value}
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && !e.shiftKey && handleSubmit()}
          placeholder="Your response…"
          aria-label="Clarification response"
          autoFocus
          className="flex-1 h-9 px-3 rounded-md bg-surface-base border border-surface-borderLight text-label text-text-body placeholder:text-text-disabled focus:outline-none focus:border-brand-primary"
        />
        <button
          onClick={handleSubmit}
          disabled={submitting || !value.trim()}
          className="h-9 px-4 rounded-md bg-brand-primary text-white text-label font-medium hover:bg-brand-primary-hover disabled:opacity-50 transition-colors"
        >
          {submitting ? '…' : 'Submit'}
        </button>
      </div>
    </div>
  )
}

// ── Integrations button ────────────────────────────────────────────────────────

const LEAD_GEN_URL = leadGenBaseUrl()

function IntegrationsButton() {
  const sessionId            = useSessionStore((s) => s.sessionId)
  const integrationsOpen     = useSessionStore((s) => s.integrationsModalOpen)
  const openIntegrationsModal = useSessionStore((s) => s.openIntegrationsModal)

  const [connectedCount, setConnectedCount] = useState(0)

  const fetchStatus = useCallback(async () => {
    if (!sessionId) return
    try {
      const res = await fetch(
        `${LEAD_GEN_URL}/tool-settings/status?tenant_id=${encodeURIComponent(sessionId)}`,
      )
      if (!res.ok) return
      const data = await res.json()
      const count = Object.values(data.tools ?? {}).filter(
        (t) => (t as { connected: boolean }).connected,
      ).length
      setConnectedCount(count)
    } catch { /* lead-gen may not be running */ }
  }, [sessionId])

  useEffect(() => { void fetchStatus() }, [fetchStatus])
  useEffect(() => { if (!integrationsOpen) void fetchStatus() }, [integrationsOpen, fetchStatus])

  return (
    <button
      onClick={openIntegrationsModal}
      aria-label="Manage integrations"
      title="Connect tools your agents can use"
      className="relative flex items-center gap-1.5 h-8 px-3 rounded-md bg-surface-overlay border border-surface-borderLight text-[12px] font-medium text-text-body hover:border-surface-muted transition-colors"
    >
      <span>⚡</span>
      <span>Integrations</span>
      {connectedCount > 0 && (
        <span className="flex items-center justify-center size-4 rounded-full bg-semantic-success text-[9px] font-bold text-white leading-none">
          {connectedCount}
        </span>
      )}
    </button>
  )
}
