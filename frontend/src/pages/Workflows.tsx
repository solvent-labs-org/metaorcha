import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Sidebar } from '../components/layout/Sidebar'
import { Badge } from '../components/ui/Badge'
import { workflows } from '../api/client'
import type { WorkflowResponse, WorkflowStatus } from '../types'
import { cn } from '../components/ui/cn'
import { A2aComposePanel } from '../components/workflows/A2aComposePanel'

type FilterTab = 'All' | 'Active' | 'Inactive' | 'Scheduled'
const TABS: FilterTab[] = ['All', 'Active', 'Inactive', 'Scheduled']

function statusBadgeVariant(status: WorkflowStatus) {
  if (status === 'active') return 'active'
  if (status === 'scheduled') return 'running'
  return 'pending'
}

function formatRelativeTime(iso: string): string {
  const diff = Date.now() - new Date(iso).getTime()
  const mins = Math.floor(diff / 60000)
  if (mins < 60) return `${mins} min ago`
  const hrs = Math.floor(mins / 60)
  if (hrs < 24) return `${hrs} hr ago`
  return `${Math.floor(hrs / 24)} day ago`
}

export function Workflows() {
  const [activeTab, setActiveTab] = useState<FilterTab>('All')
  const [search, setSearch] = useState('')
  const qc = useQueryClient()

  const { data, isLoading } = useQuery({
    queryKey: ['workflows'],
    queryFn: () => workflows.list(),
  })

  const deleteMut = useMutation({
    mutationFn: (id: string) => workflows.delete(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['workflows'] }),
  })

  const updateMut = useMutation({
    mutationFn: ({ id, patch }: { id: string; patch: { status: WorkflowStatus } }) =>
      workflows.update(id, patch),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['workflows'] }),
  })

  const filtered = (data ?? []).filter((wf) => {
    const matchTab =
      activeTab === 'All' || wf.status.toLowerCase() === activeTab.toLowerCase()
    const matchSearch = wf.name.toLowerCase().includes(search.toLowerCase())
    return matchTab && matchSearch
  })

  return (
    <div className="flex h-screen bg-surface-canvas overflow-hidden">
      <Sidebar />

      <main className="flex-1 ml-16 flex flex-col overflow-hidden">
        {/* Page header */}
        <div className="h-16 flex items-center px-6 bg-surface-base border-b border-surface-border shrink-0">
          <h1 className="text-h2 text-text-heading flex-1">My Workflows</h1>
          <div className="relative">
            <input
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Search workflows…"
              aria-label="Search workflows"
              className="w-[280px] h-9 pl-3 pr-3 rounded-md bg-surface-elevated border border-surface-border text-label text-text-body placeholder:text-text-disabled focus:outline-none focus:border-brand-primary"
            />
          </div>
        </div>

        {/* Status tab bar */}
        <div className="flex items-center gap-1 px-4 h-10 bg-surface-elevated border-b border-surface-border shrink-0">
          {TABS.map((tab) => (
            <button
              key={tab}
              onClick={() => setActiveTab(tab)}
              role="tab"
              aria-selected={activeTab === tab}
              className={cn(
                'h-8 px-3 rounded-sm text-[13px] transition-colors duration-150',
                activeTab === tab
                  ? 'bg-brand-primary-dim text-brand-primary-light font-medium border-b-2 border-brand-primary'
                  : 'text-text-secondary hover:text-text-body',
              )}
            >
              {tab}
            </button>
          ))}
        </div>

        {/* Workflow list */}
        <div className="flex-1 overflow-y-auto scrollbar-thin px-6 py-4 flex flex-col gap-3">
          <A2aComposePanel />

          {isLoading && (
            <div className="text-text-secondary text-body-md">Loading…</div>
          )}

          {!isLoading && filtered.length === 0 && (
            <div className="text-text-secondary text-body-md">No workflows found.</div>
          )}

          {filtered.map((wf) => (
            <WorkflowCard
              key={wf.id}
              workflow={wf}
              onDelete={() => deleteMut.mutate(wf.id)}
              onToggleStatus={() =>
                updateMut.mutate({
                  id: wf.id,
                  patch: { status: wf.status === 'active' ? 'inactive' : 'active' },
                })
              }
            />
          ))}
        </div>
      </main>
    </div>
  )
}

function WorkflowCard({
  workflow,
  onDelete,
  onToggleStatus,
}: {
  workflow: WorkflowResponse
  onDelete: () => void
  onToggleStatus: () => void
}) {
  return (
    <div className="flex items-center gap-3 h-[72px] px-4 rounded-md bg-surface-elevated border border-surface-border">
      {/* Status dot */}
      <span
        className={cn(
          'size-2 rounded-full shrink-0',
          workflow.status === 'active' ? 'bg-semantic-success' : 'bg-surface-muted',
        )}
        aria-hidden="true"
      />

      {/* Name + tags */}
      <div className="flex-1 min-w-0 flex items-center gap-2">
        <span className="text-body-md font-semibold text-text-heading truncate">
          {workflow.name}
        </span>
        {workflow.agents_used.slice(0, 2).map((tag) => (
          <span
            key={tag}
            className="h-5 px-1.5 rounded-xs bg-surface-overlay border border-surface-border text-[11px] text-text-secondary shrink-0"
          >
            {tag}
          </span>
        ))}
      </div>

      {/* Last run */}
      <span className="text-[12px] text-text-secondary shrink-0 ml-auto pr-4">
        {workflow.updated_at ? formatRelativeTime(workflow.updated_at) : '—'}
      </span>

      {/* Status badge */}
      <Badge variant={statusBadgeVariant(workflow.status)} className="shrink-0" />

      {/* Actions */}
      <div className="flex items-center gap-2 ml-3 shrink-0">
        <button
          onClick={onToggleStatus}
          aria-label={workflow.status === 'active' ? 'Deactivate workflow' : 'Activate workflow'}
          className="text-surface-muted hover:text-text-body transition-colors text-sm"
          title={workflow.status === 'active' ? 'Deactivate' : 'Activate'}
        >
          {workflow.status === 'active' ? '⏸' : '▶'}
        </button>
        <button
          onClick={onDelete}
          aria-label="Delete workflow"
          className="text-surface-muted hover:text-semantic-error transition-colors text-sm"
          title="Delete"
        >
          🗑
        </button>
      </div>
    </div>
  )
}
