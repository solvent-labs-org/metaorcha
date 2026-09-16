import { useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { credentials, devAgents } from '../../api/client'
import { buildMcpEmergeYaml } from '../../lib/mcpEmergeYaml'
import { Button } from '../ui/Button'
import { cn } from '../ui/cn'

interface PluginsModalProps {
  open: boolean
  onClose: () => void
}

export function PluginsModal({ open, onClose }: PluginsModalProps) {
  const qc = useQueryClient()
  const [name, setName] = useState('')
  const [transport, setTransport] = useState<'sse' | 'stdio'>('sse')
  const [endpoint, setEndpoint] = useState('')
  const [command, setCommand] = useState('')
  const [authValue, setAuthValue] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const authVar = 'MCP_TOKEN'

  if (!open) return null

  const handleConnect = async () => {
    setError(null)
    setBusy(true)
    try {
      const yaml = buildMcpEmergeYaml({
        name,
        transport,
        endpoint: transport === 'sse' ? endpoint : undefined,
        command: transport === 'stdio' ? command : undefined,
        authVar: authValue.trim() ? authVar : undefined,
      })
      const file = new File([yaml], 'emerge.yaml', { type: 'text/yaml' })
      const res = await devAgents.register(file)
      const agentId = res.data.agent_id
      if (authValue.trim()) {
        await credentials.set({
          agent_id: agentId,
          var_name: authVar,
          value: authValue.trim(),
          scope: 'permanent',
        })
      }
      void qc.invalidateQueries({ queryKey: ['dev-agents'] })
      onClose()
      setName('')
      setEndpoint('')
      setCommand('')
      setAuthValue('')
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Connect failed')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center">
      <button
        type="button"
        className="absolute inset-0 bg-black/50"
        aria-label="Close plugins"
        onClick={onClose}
      />
      <div
        role="dialog"
        aria-labelledby="plugins-title"
        className="relative w-[440px] rounded-lg border border-surface-border bg-surface-elevated shadow-lg"
      >
        <div className="flex h-14 items-center border-b border-surface-border px-5">
          <p id="plugins-title" className="text-[15px] font-semibold text-text-heading">
            Bring an MCP
          </p>
        </div>
        <div className="flex flex-col gap-3 px-5 py-4">
          <p className="text-caption text-text-secondary">
            Connect one of yours. This is not a catalogue. Auth is stored in the vault, not
            the manifest.
          </p>
          <label className="flex flex-col gap-1">
            <span className="text-[11px] text-text-secondary">Name</span>
            <input
              value={name}
              onChange={(e) => setName(e.target.value)}
              className={fieldClass}
              placeholder="Docs MCP"
            />
          </label>
          <label className="flex flex-col gap-1">
            <span className="text-[11px] text-text-secondary">Transport</span>
            <select
              value={transport}
              onChange={(e) => setTransport(e.target.value as 'sse' | 'stdio')}
              className={fieldClass}
            >
              <option value="sse">SSE URL</option>
              <option value="stdio">stdio command</option>
            </select>
          </label>
          {transport === 'sse' ? (
            <label className="flex flex-col gap-1">
              <span className="text-[11px] text-text-secondary">Endpoint</span>
              <input
                value={endpoint}
                onChange={(e) => setEndpoint(e.target.value)}
                className={fieldClass}
                placeholder="https://example.com/mcp"
              />
            </label>
          ) : (
            <label className="flex flex-col gap-1">
              <span className="text-[11px] text-text-secondary">Command</span>
              <input
                value={command}
                onChange={(e) => setCommand(e.target.value)}
                className={fieldClass}
                placeholder="npx -y my-mcp"
              />
            </label>
          )}
          <label className="flex flex-col gap-1">
            <span className="text-[11px] text-text-secondary">Auth token (optional)</span>
            <input
              type="password"
              value={authValue}
              onChange={(e) => setAuthValue(e.target.value)}
              className={fieldClass}
              placeholder="stored as MCP_TOKEN"
            />
          </label>
          {error && <p className="text-caption text-semantic-error">{error}</p>}
        </div>
        <div className="flex gap-2 border-t border-surface-border px-5 py-4">
          <Button variant="ghost" className="flex-1" onClick={onClose}>
            Cancel
          </Button>
          <Button
            className="flex-1"
            loading={busy}
            disabled={
              !name.trim() ||
              busy ||
              (transport === 'sse' ? !endpoint.trim() : !command.trim())
            }
            onClick={() => void handleConnect()}
          >
            Connect
          </Button>
        </div>
      </div>
    </div>
  )
}

const fieldClass = cn(
  'h-9 rounded-md border border-surface-borderLight bg-surface-base px-3 text-label text-text-body',
  'placeholder:text-text-disabled focus:border-brand-primary focus:outline-none',
)
