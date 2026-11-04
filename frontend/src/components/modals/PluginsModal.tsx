import { useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { plugins } from '../../api/client'
import { Button } from '../ui/Button'
import { cn } from '../ui/cn'

interface PluginsModalProps {
  open: boolean
  onClose: () => void
}

// NFR-15: the documented default for a GitHub connection is the smallest
// fine-grained token that can read a repository and open a pull request.
// There is deliberately no OAuth path: the token is the user's own, scoped by
// them on GitHub and revocable there.
const GITHUB_PERMISSIONS = [
  'Metadata: Read-only',
  'Contents: Read-only',
  'Issues: Read-only',
  'Pull requests: Read and write',
]

const AUTH_VAR_RE = /^[A-Z_][A-Z0-9_]{0,63}$/

export function PluginsModal({ open, onClose }: PluginsModalProps) {
  const qc = useQueryClient()
  const [name, setName] = useState('')
  const [transport, setTransport] = useState<'sse' | 'stdio'>('sse')
  const [endpoint, setEndpoint] = useState('')
  const [command, setCommand] = useState('')
  const [authVar, setAuthVar] = useState('MCP_TOKEN')
  const [authValue, setAuthValue] = useState('')
  const [preset, setPreset] = useState<'github' | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const authVarValid = AUTH_VAR_RE.test(authVar.trim())

  if (!open) return null

  const handleConnect = async () => {
    setError(null)
    setBusy(true)
    try {
      // One JSON call. The Gateway writes the manifest and stores the token
      // in the vault before registering; the token never touches the browser
      // beyond this request body.
      const token = authValue.trim()
      await plugins.connectMcp({
        name: name.trim(),
        transport,
        endpoint: transport === 'sse' ? endpoint.trim() : undefined,
        command: transport === 'stdio' ? command.trim() : undefined,
        ...(token ? { auth_var: authVar.trim(), auth_value: token } : {}),
      })
      void qc.invalidateQueries({ queryKey: ['dev-agents'] })
      onClose()
      setName('')
      setEndpoint('')
      setCommand('')
      setAuthVar('MCP_TOKEN')
      setAuthValue('')
      setPreset(null)
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
          <div className="flex items-center gap-2">
            <span className="text-[11px] text-text-secondary">Preset</span>
            <Button
              variant="ghost"
              onClick={() => {
                setPreset('github')
                setName('GitHub')
                setTransport('sse')
                setAuthVar('GITHUB_TOKEN')
              }}
            >
              GitHub
            </Button>
          </div>
          {preset === 'github' && (
            <div className="rounded-md border border-surface-borderLight px-3 py-2 text-caption text-text-secondary">
              <p>
                Use your own fine-grained personal access token, limited to the repositories
                this connection may touch, with only these repository permissions:
              </p>
              <ul className="mt-1 list-disc pl-4">
                {GITHUB_PERMISSIONS.map((perm) => (
                  <li key={perm}>{perm}</li>
                ))}
              </ul>
              <p className="mt-1">
                There is no sign-in with GitHub here: you create the token, and you can revoke
                it on GitHub at any time.
              </p>
            </div>
          )}
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
            <span className="text-[11px] text-text-secondary">Token variable</span>
            <input
              value={authVar}
              onChange={(e) => setAuthVar(e.target.value.toUpperCase())}
              className={fieldClass}
              placeholder="MCP_TOKEN"
            />
          </label>
          <label className="flex flex-col gap-1">
            <span className="text-[11px] text-text-secondary">Auth token (optional)</span>
            <input
              type="password"
              value={authValue}
              onChange={(e) => setAuthValue(e.target.value)}
              className={fieldClass}
              placeholder={`stored as ${authVar.trim() || 'MCP_TOKEN'}`}
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
              (transport === 'sse' ? !endpoint.trim() : !command.trim()) ||
              (authValue.trim() !== '' && !authVarValid)
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
