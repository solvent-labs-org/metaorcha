import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { workflows } from '../../api/client'
import { isComputerUseTrace } from '../../lib/computerUse'
import { downloadReceipt } from '../../lib/downloadReceipt'
import { useSessionStore } from '../../store/session'
import { AgentCard } from '../agents/AgentCard'
import { ComputerUseViewport } from '../chat/ComputerUseViewport'
import { RoutineEditorModal } from '../modals/RoutineEditorModal'
import type { FiringResponse, WorkflowResponse } from '../../types'
import { Button } from '../ui/Button'
import { cn } from '../ui/cn'

type Section = 'Routines' | 'Agents' | 'Computer use'
const SECTIONS: Section[] = ['Routines', 'Agents', 'Computer use']

export function RightPanel() {
  const [active, setActive] = useState<Section>('Routines')
  const agents = useSessionStore((s) => s.agents)
  const toolTrace = useSessionStore((s) => s.toolTrace)
  const sessionId = useSessionStore((s) => s.sessionId)
  const openSaveWorkflow = useSessionStore((s) => s.openSaveWorkflowModal)

  const cuTrace = [...toolTrace].reverse().find(isComputerUseTrace)

  return (
    <aside
      className="flex h-full w-80 flex-col border-l border-surface-border bg-surface-base"
      aria-label="Routines, agents, computer use"
    >
      <div className="flex h-14 shrink-0 items-center border-b border-surface-border bg-surface-elevated px-4">
        <span className="text-label font-semibold text-text-heading">Workspace</span>
      </div>

      <div
        className="flex shrink-0 items-center gap-1 border-b border-surface-border bg-surface-elevated px-2 py-1.5"
        role="tablist"
        aria-label="Workspace sections"
      >
        {SECTIONS.map((tab) => (
          <button
            key={tab}
            type="button"
            role="tab"
            aria-selected={active === tab}
            onClick={() => setActive(tab)}
            className={cn(
              'h-8 flex-1 rounded-sm text-[11px] transition-colors duration-150',
              active === tab
                ? 'bg-brand-primary-dim font-medium text-brand-primary-light'
                : 'text-text-secondary hover:text-text-body',
            )}
          >
            {tab}
          </button>
        ))}
      </div>

      <div className="flex-1 overflow-y-auto py-3 scrollbar-thin">
        {active === 'Routines' && <RoutinesSection />}
        {active === 'Agents' && (
          <div>
            <p className="mb-2 px-4 text-[10px] font-semibold uppercase tracking-caps text-text-disabled">
              Session agents
            </p>
            {agents.length === 0 ? (
              <p className="px-4 text-caption text-text-disabled">None on this turn yet</p>
            ) : (
              <div className="flex flex-col gap-2 px-3">
                {agents.map((agent) => (
                  <AgentCard key={agent.agent_id} agent={agent} />
                ))}
              </div>
            )}
          </div>
        )}
        {active === 'Computer use' && (
          <div className="px-3">
            {cuTrace ? (
              <ComputerUseViewport trace={cuTrace} />
            ) : (
              <p className="text-caption text-text-disabled">
                Computer-use frames from this run appear here. They also stay inline in the
                transcript.
              </p>
            )}
          </div>
        )}
      </div>

      {sessionId && active === 'Routines' && (
        <div className="shrink-0 border-t border-surface-border p-3">
          <Button variant="primary" className="w-full" onClick={openSaveWorkflow}>
            Save this chat as a routine
          </Button>
        </div>
      )}
    </aside>
  )
}

function RoutineItem({ wf }: { wf: WorkflowResponse }) {
  const qc = useQueryClient()
  const [error, setError] = useState<string | null>(null)
  const toggle = useMutation({
    mutationFn: (on: boolean) =>
      workflows.update(wf.id, { status: on ? 'scheduled' : 'inactive' }),
    onSuccess: () => {
      setError(null)
      void qc.invalidateQueries({ queryKey: ['workflows'] })
    },
    onError: (e: unknown) => setError(e instanceof Error ? e.message : 'Could not change the schedule'),
  })
  const firing = wf.last_firing
  const scheduled = Boolean(wf.schedule_enabled)

  return (
    <li className="rounded-md border border-surface-border bg-surface-overlay px-2.5 py-2">
      <p className="truncate text-label font-medium text-text-body">{wf.name}</p>
      <p className="truncate font-mono text-[10px] text-text-disabled">
        {wf.schedule_cron
          ? `${wf.schedule_cron} ${wf.schedule_tz ?? ''}`.trim()
          : wf.agents_used.length > 0
            ? wf.agents_used.join(' · ')
            : wf.status}
      </p>
      {wf.schedule_cron && (
        <p className="mt-1 flex items-center gap-2 text-[10px] text-text-secondary">
          <span>
            {scheduled && wf.next_run_at
              ? `next ${new Date(wf.next_run_at).toLocaleString()}`
              : 'schedule off'}
          </span>
          <button
            type="button"
            disabled={toggle.isPending}
            onClick={() => toggle.mutate(!scheduled)}
            className="text-brand-primary-light hover:underline disabled:opacity-50"
          >
            {scheduled ? 'Pause' : 'Turn on'}
          </button>
        </p>
      )}
      {firing && <FiringLines firing={firing} />}
      {error && <p className="mt-1 text-[10px] text-semantic-error">{error}</p>}
    </li>
  )
}

// Story 2.5: the pane renders what the server computed for the firing (its
// label, the gate and checks qualifiers, the note) and never words a state
// itself. Colour is keyed on the row's state; that is styling, not wording.
function FiringLines({ firing }: { firing: FiringResponse }) {
  const [receiptError, setReceiptError] = useState<string | null>(null)
  const [receiptPending, setReceiptPending] = useState(false)
  const runId = firing.run_id

  const onReceipt = async () => {
    if (!runId) return
    setReceiptPending(true)
    setReceiptError(null)
    try {
      await downloadReceipt(runId)
    } catch (e: unknown) {
      setReceiptError(e instanceof Error ? e.message : 'Could not download the receipt')
    } finally {
      setReceiptPending(false)
    }
  }

  return (
    <>
      <p
        className={cn(
          'mt-1 text-[10px]',
          firing.state === 'error' || firing.state === 'refused'
            ? 'text-semantic-error'
            : 'text-text-secondary',
        )}
      >
        last: {firing.label}
        {firing.gate_label ? ` · ${firing.gate_label}` : ''}
        {firing.checks_label ? ` · ${firing.checks_label}` : ''}
      </p>
      {firing.note && (
        <p className="truncate text-[10px] text-text-disabled" title={firing.note}>
          {firing.note}
        </p>
      )}
      {(firing.session_id || firing.receipt_available) && (
        <p className="mt-1 flex items-center gap-2 text-[10px] text-text-secondary">
          {firing.session_id && (
            <Link
              to={`/chat/${firing.session_id}`}
              className="text-brand-primary-light hover:underline"
            >
              {firing.state === 'paused' ? 'Review approval' : 'Open'}
            </Link>
          )}
          {firing.receipt_downloadable && runId ? (
            <button
              type="button"
              disabled={receiptPending}
              onClick={() => void onReceipt()}
              className="text-brand-primary-light hover:underline disabled:opacity-50"
            >
              Receipt
            </button>
          ) : firing.receipt_available && !firing.receipt_downloadable ? (
            <span className="text-text-disabled">receipt in the owner&apos;s session</span>
          ) : null}
        </p>
      )}
      {receiptError && (
        <p className="mt-1 truncate text-[10px] text-semantic-error" title={receiptError}>
          {receiptError}
        </p>
      )}
    </>
  )
}

function RoutinesSection() {
  const { data, isLoading } = useQuery({
    queryKey: ['workflows'],
    queryFn: () => workflows.list(),
    // A running firing must not sit stale behind the 30 s staleTime until
    // the window is refocused.
    refetchInterval: 30_000,
  })
  const items = data ?? []
  const [editorOpen, setEditorOpen] = useState(false)

  return (
    <div>
      <RoutineEditorModal open={editorOpen} onClose={() => setEditorOpen(false)} />
      <p className="mb-1 px-4 text-[10px] font-semibold uppercase tracking-caps text-text-disabled">
        Routines
      </p>
      {/* FR-26 / NFR-14: what a restart costs, said once. 60 s is the
          scheduler's default interval. */}
      <p className="mb-2 px-4 text-[10px] text-text-disabled">
        Routines run inside the server process, which checks for due routines every 60 seconds;
        a restart ends any firing still running without a sealed receipt as error (detail:
        restart), may strand one waiting for approval, and drops any settlement not yet
        recorded.
      </p>
      {isLoading && <p className="px-4 text-caption text-text-secondary">Loading…</p>}
      {!isLoading && items.length === 0 && (
        <p className="px-4 text-caption text-text-disabled">
          No routines yet. Save a chat, or wire two of your agents when compose lands.
        </p>
      )}
      <ul className="m-0 flex list-none flex-col gap-1.5 px-3 p-0">
        {items.map((wf) => (
          <RoutineItem key={wf.id} wf={wf} />
        ))}
      </ul>
      <p className="mt-3 flex items-center gap-3 px-4">
        <button
          type="button"
          onClick={() => setEditorOpen(true)}
          className="text-[11px] text-brand-primary-light hover:underline"
        >
          New routine
        </button>
        <Link to="/workflows" className="text-[11px] text-brand-primary-light hover:underline">
          Open all routines
        </Link>
      </p>
    </div>
  )
}
