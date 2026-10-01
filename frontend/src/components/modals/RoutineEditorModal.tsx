import { useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { devAgents, workflows } from '../../api/client'
import type { RoutineRejection } from '../../types'
import { Button } from '../ui/Button'

// Story 2.1 (FR-23, UX-DR4): a routine is a goal, its connections, the writes
// it may make without asking, a model, declared criteria and a cron. The
// Gateway and SuperAgent decide what is allowed; this form only collects it
// and shows a refusal with the field it names. Every capability is listed; a
// destructive one (or one the class rules do not know) is refused at save.

const CONNECTION_PREFIX = 'did:orcha:agent:'
const CRITERIA = ['citations_required'] as const

const inputClass =
  'h-9 px-3 rounded-md bg-surface-base border border-surface-borderLight text-label text-text-body placeholder:text-text-disabled focus:outline-none focus:border-brand-primary'

function capabilityIds(manifest: Record<string, unknown> | undefined): string[] {
  const data = (manifest?.data ?? manifest) as Record<string, unknown> | undefined
  const caps = data?.capabilities
  if (!Array.isArray(caps)) return []
  return caps
    .map((c) => (c && typeof c === 'object' ? String((c as Record<string, unknown>).capability_id ?? (c as Record<string, unknown>).id ?? '') : ''))
    .filter(Boolean)
}

function parseRejection(e: unknown): RoutineRejection {
  const message = e instanceof Error ? e.message : String(e)
  const body = message.replace(/^\d+:\s*/, '')
  try {
    const detail = JSON.parse(body)?.detail
    if (detail && typeof detail === 'object' && 'reason' in detail) {
      return { field: String(detail.field ?? 'routine'), reason: String(detail.reason) }
    }
    if (typeof detail === 'string') return { field: 'routine', reason: detail }
    if (Array.isArray(detail) && detail[0]) {
      // request-shape errors: { loc: ['body', '<field>'], msg }
      const loc = Array.isArray(detail[0].loc) ? detail[0].loc : []
      return { field: String(loc[loc.length - 1] ?? 'routine'), reason: String(detail[0].msg ?? 'invalid') }
    }
  } catch {
    // not JSON — fall through
  }
  return { field: 'routine', reason: message || 'The routine was not saved' }
}

function ConnectionAllows({
  did,
  allow,
  onToggle,
}: {
  did: string
  allow: string[]
  onToggle: (entry: string) => void
}) {
  const { data, isLoading } = useQuery({
    queryKey: ['agent-manifest', did],
    queryFn: () => devAgents.get(did),
  })
  const caps = capabilityIds(data)
  if (isLoading) return <p className="text-caption text-text-secondary">Loading capabilities…</p>
  if (caps.length === 0) return <p className="text-caption text-text-disabled">No capabilities listed.</p>
  return (
    <ul className="m-0 flex list-none flex-col gap-1 p-0">
      {caps.map((cap) => {
        const entry = `${did}#${cap}`
        return (
          <li key={entry}>
            <label className="flex items-center gap-2 font-mono text-[11px] text-text-body">
              <input type="checkbox" checked={allow.includes(entry)} onChange={() => onToggle(entry)} />
              {cap}
            </label>
          </li>
        )
      })}
    </ul>
  )
}

export function RoutineEditorModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const queryClient = useQueryClient()
  const [name, setName] = useState('')
  const [goal, setGoal] = useState('')
  const [connections, setConnections] = useState<string[]>([])
  const [allow, setAllow] = useState<string[]>([])
  const [model, setModel] = useState('')
  const [criteria, setCriteria] = useState<Record<string, boolean>>({})
  const [cron, setCron] = useState('0 9 * * 1')
  const [timezone, setTimezone] = useState(
    () => Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC',
  )
  const [saving, setSaving] = useState(false)
  const [rejection, setRejection] = useState<RoutineRejection | null>(null)

  const { data: agentList } = useQuery({
    queryKey: ['dev-agents', 'routine-editor'],
    queryFn: () => devAgents.list({ page: 1, limit: 50 }),
    enabled: open,
  })
  const candidates = (agentList?.data.agents ?? []).filter((a) => a.id.startsWith(CONNECTION_PREFIX))

  if (!open) return null

  const toggleConnection = (did: string) => {
    setConnections((cur) => (cur.includes(did) ? cur.filter((d) => d !== did) : [...cur, did]))
    setAllow((cur) => cur.filter((entry) => !entry.startsWith(`${did}#`)))
  }
  const toggleAllow = (entry: string) =>
    setAllow((cur) => (cur.includes(entry) ? cur.filter((e) => e !== entry) : [...cur, entry]))

  const canSave = name.trim() && goal.trim() && model.trim() && cron.trim() && connections.length > 0

  const handleSave = async () => {
    if (!canSave) return
    setSaving(true)
    setRejection(null)
    try {
      await workflows.createRoutine({
        name: name.trim(),
        goal: goal.trim(),
        connections,
        scope_allow: allow,
        model: model.trim(),
        criteria,
        // Operands are accepted by the API; no criterion reads one yet (2.3).
        criteria_operands: {},
        cron: cron.trim(),
        timezone: timezone.trim() || 'UTC',
      })
      await queryClient.invalidateQueries({ queryKey: ['workflows'] })
      onClose()
    } catch (e) {
      setRejection(parseRejection(e))
    } finally {
      setSaving(false)
    }
  }

  const fieldError = (field: string) =>
    rejection?.field === field ? (
      <p role="alert" className="text-caption text-semantic-error">{rejection.reason}</p>
    ) : null

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center" role="dialog" aria-modal="true" aria-label="New routine">
      <div className="absolute inset-0 bg-surface-canvas/70" onClick={onClose} aria-hidden="true" />
      <div className="relative flex max-h-[90vh] w-[520px] flex-col gap-4 overflow-y-auto rounded-lg border border-surface-border bg-surface-elevated p-6 shadow-lg">
        <div className="flex items-center justify-between">
          <h2 className="text-h3 text-text-heading">New routine</h2>
          <button onClick={onClose} aria-label="Close" className="text-lg text-text-secondary hover:text-text-body">×</button>
        </div>

        <div className="flex flex-col gap-1">
          <label htmlFor="rt-name" className="text-caption font-medium text-text-secondary">Name *</label>
          <input id="rt-name" value={name} onChange={(e) => setName(e.target.value)} className={inputClass} />
        </div>

        <div className="flex flex-col gap-1">
          <label htmlFor="rt-goal" className="text-caption font-medium text-text-secondary">Goal *</label>
          <textarea
            id="rt-goal"
            value={goal}
            onChange={(e) => setGoal(e.target.value)}
            rows={3}
            placeholder="What should this routine do each time it runs?"
            className="resize-none rounded-md border border-surface-borderLight bg-surface-base px-3 py-2 text-label text-text-body placeholder:text-text-disabled focus:border-brand-primary focus:outline-none"
          />
        </div>

        <fieldset className="flex flex-col gap-2">
          <legend className="text-caption font-medium text-text-secondary">Connections *</legend>
          {candidates.length === 0 && (
            <p className="text-caption text-text-disabled">No connections yet. Connect a service first.</p>
          )}
          {candidates.map((a) => (
            <div key={a.id} className="rounded-md border border-surface-border px-2.5 py-2">
              <label className="flex items-center gap-2 text-label text-text-body">
                <input type="checkbox" checked={connections.includes(a.id)} onChange={() => toggleConnection(a.id)} />
                {a.name}
              </label>
              {connections.includes(a.id) && (
                <div className="mt-2 pl-6">
                  <p className="mb-1 text-[10px] uppercase tracking-caps text-text-disabled">
                    Allowed without asking
                  </p>
                  <ConnectionAllows did={a.id} allow={allow} onToggle={toggleAllow} />
                </div>
              )}
            </div>
          ))}
          {fieldError('connections')}
          {fieldError('scope_allow')}
        </fieldset>

        <div className="flex flex-col gap-1">
          <label htmlFor="rt-model" className="text-caption font-medium text-text-secondary">Model *</label>
          <input id="rt-model" value={model} onChange={(e) => setModel(e.target.value)} className={inputClass} />
          {fieldError('model')}
        </div>

        <fieldset className="flex flex-col gap-2">
          <legend className="text-caption font-medium text-text-secondary">Criteria</legend>
          {CRITERIA.map((key) => (
            <label key={key} className="flex items-center gap-2 font-mono text-[11px] text-text-body">
              <input
                type="checkbox"
                checked={criteria[key] === true}
                onChange={(e) =>
                  setCriteria((cur) => {
                    const next = { ...cur }
                    if (e.target.checked) next[key] = true
                    else delete next[key]
                    return next
                  })
                }
              />
              {key}
            </label>
          ))}
          {fieldError('criteria')}
          {fieldError('criteria_operands')}
        </fieldset>

        <div className="flex gap-3">
          <div className="flex flex-1 flex-col gap-1">
            <label htmlFor="rt-cron" className="text-caption font-medium text-text-secondary">Schedule (cron) *</label>
            <input id="rt-cron" value={cron} onChange={(e) => setCron(e.target.value)} className={`${inputClass} font-mono`} />
            {fieldError('cron')}
          </div>
          <div className="flex flex-1 flex-col gap-1">
            <label htmlFor="rt-tz" className="text-caption font-medium text-text-secondary">Time zone</label>
            <input id="rt-tz" value={timezone} onChange={(e) => setTimezone(e.target.value)} className={inputClass} />
            {fieldError('timezone')}
          </div>
        </div>
        <p className="text-caption text-text-disabled">
          Saved routines are not scheduled to run yet.
        </p>

        {rejection && !['connections', 'scope_allow', 'model', 'criteria', 'criteria_operands', 'cron', 'timezone'].includes(rejection.field) && (
          <p role="alert" className="text-caption text-semantic-error">{rejection.reason}</p>
        )}

        <div className="flex justify-end gap-2">
          <Button variant="ghost" onClick={onClose}>Cancel</Button>
          <Button onClick={handleSave} loading={saving} disabled={!canSave}>Save routine</Button>
        </div>
      </div>
    </div>
  )
}
