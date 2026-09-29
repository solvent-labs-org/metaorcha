import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { workflows } from '../../api/client'
import { isComputerUseTrace } from '../../lib/computerUse'
import { useSessionStore } from '../../store/session'
import { AgentCard } from '../agents/AgentCard'
import { ComputerUseViewport } from '../chat/ComputerUseViewport'
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

function RoutinesSection() {
  const { data, isLoading } = useQuery({
    queryKey: ['workflows'],
    queryFn: () => workflows.list(),
  })
  const items = data ?? []

  return (
    <div>
      <p className="mb-2 px-4 text-[10px] font-semibold uppercase tracking-caps text-text-disabled">
        Routines
      </p>
      {isLoading && <p className="px-4 text-caption text-text-secondary">Loading…</p>}
      {!isLoading && items.length === 0 && (
        <p className="px-4 text-caption text-text-disabled">
          No routines yet. Save a chat, or wire two of your agents when compose lands.
        </p>
      )}
      <ul className="m-0 flex list-none flex-col gap-1.5 px-3 p-0">
        {items.map((wf) => (
          <li
            key={wf.id}
            className="rounded-md border border-surface-border bg-surface-overlay px-2.5 py-2"
          >
            <p className="truncate text-label font-medium text-text-body">{wf.name}</p>
            <p className="truncate font-mono text-[10px] text-text-disabled">
              {wf.agents_used.length > 0 ? wf.agents_used.join(' · ') : wf.status}
            </p>
          </li>
        ))}
      </ul>
      <p className="mt-3 px-4">
        <Link to="/workflows" className="text-[11px] text-brand-primary-light hover:underline">
          Open all routines
        </Link>
      </p>
    </div>
  )
}
