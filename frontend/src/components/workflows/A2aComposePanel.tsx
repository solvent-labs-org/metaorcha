import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { devAgents, workflows } from '../../api/client'
import { Button } from '../ui/Button'

/** Wire ≥2 of the user's registered agents into a routine. Not a protocol trophy. */
export function A2aComposePanel() {
  const qc = useQueryClient()
  const [name, setName] = useState('')
  const [picked, setPicked] = useState<string[]>([])
  const [error, setError] = useState<string | null>(null)

  const { data } = useQuery({
    queryKey: ['dev-agents', 'compose'],
    queryFn: () => devAgents.list({ page: 1, limit: 50 }),
  })
  const agents = data?.data.agents ?? []

  const compose = useMutation({
    mutationFn: () => workflows.compose(name.trim(), picked),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['workflows'] })
      setName('')
      setPicked([])
      setError(null)
    },
    onError: (e: unknown) => {
      setError(e instanceof Error ? e.message : 'Compose failed')
    },
  })

  const toggle = (id: string) => {
    setPicked((cur) => (cur.includes(id) ? cur.filter((x) => x !== id) : [...cur, id]))
  }

  return (
    <div className="mb-4 rounded-md border border-surface-border bg-surface-elevated p-4">
      <p className="text-label font-semibold text-text-heading">Wire your own A2A</p>
      <p className="mt-1 text-caption text-text-secondary">
        Pick at least two of your agents. The harness is the bus. This is not Google A2A
        or ACP as a product.
      </p>
      <input
        value={name}
        onChange={(e) => setName(e.target.value)}
        placeholder="Routine name"
        className="mt-3 h-9 w-full rounded-md border border-surface-borderLight bg-surface-base px-3 text-label text-text-body placeholder:text-text-disabled focus:border-brand-primary focus:outline-none"
      />
      <ul className="mt-2 max-h-40 list-none overflow-y-auto p-0">
        {agents.map((a) => (
          <li key={a.id}>
            <label className="flex cursor-pointer items-center gap-2 py-1 text-label text-text-body">
              <input
                type="checkbox"
                checked={picked.includes(a.id)}
                onChange={() => toggle(a.id)}
              />
              {a.name}
              <span className="font-mono text-[10px] text-text-disabled">{a.protocol_type}</span>
            </label>
          </li>
        ))}
      </ul>
      {agents.length === 0 && (
        <p className="text-caption text-text-disabled">Register agents first, then wire them.</p>
      )}
      {error && <p className="mt-2 text-caption text-semantic-error">{error}</p>}
      <Button
        className="mt-3"
        disabled={!name.trim() || picked.length < 2 || compose.isPending}
        onClick={() => compose.mutate()}
      >
        Compose ({picked.length} agents)
      </Button>
    </div>
  )
}
