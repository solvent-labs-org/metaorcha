import { useQuery } from '@tanstack/react-query'
import { NavLink, useLocation, useNavigate } from 'react-router-dom'
import { devAgents, sessions } from '../../api/client'
import { useSessionSidebarStore } from '../../store/sessionSidebar'
import { useByokStore } from '../../store/byok'
import { cn } from '../ui/cn'
import { IconCollapseSessionPanel, IconExpandSessionPanel } from './SessionPanelToggleIcons'
import { sessionTitleFromMessage } from '../../lib/sessionTitle'

export function SessionListPanel() {
  const location = useLocation()
  const navigate = useNavigate()
  const isChatRoute = location.pathname.startsWith('/chat')

  const isOpen = useSessionSidebarStore((s) => s.isOpen)
  const setOpen = useSessionSidebarStore((s) => s.setOpen)

  const { data, isLoading, isError } = useQuery({
    queryKey: ['sessions', 1],
    queryFn: () => sessions.list({ page: 1, page_size: 30 }),
  })

  const { data: agentsData } = useQuery({
    queryKey: ['dev-agents', 'bots'],
    queryFn: () => devAgents.list({ page: 1, limit: 30 }),
  })

  const panelId = 'session-list-panel'
  const bots = agentsData?.data.agents ?? []

  const startBot = async (bot: { id: string; name: string; protocol_type: 'mcp' | 'a2a' }) => {
    const title = sessionTitleFromMessage(bot.name)
    const { session_id } = await sessions.create({ title })
    void useByokStore.getState().applyToSession(session_id)
    navigate(`/chat/${session_id}`, {
      state: {
        bot: {
          agent_id: bot.id,
          name: bot.name,
          type: bot.protocol_type,
          status: 'pending' as const,
        },
      },
    })
  }

  return (
    <>
      {isOpen ? null : (
        <button
          type="button"
          onClick={() => setOpen(true)}
          aria-label="Open bots"
          aria-expanded={false}
          aria-controls={panelId}
          className={cn(
            'fixed left-16 z-20 flex items-center justify-center border-surface-border bg-surface-base text-text-secondary transition-colors hover:bg-surface-overlay hover:text-text-body focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-primary',
            isChatRoute
              ? 'top-0 h-14 w-10 border-b border-r'
              : 'top-14 size-9 rounded-md border',
          )}
          title="Open bots"
        >
          <IconExpandSessionPanel className="shrink-0" />
        </button>
      )}

      <aside
        id={panelId}
        aria-label="Bots and sessions"
        aria-hidden={!isOpen}
        className={cn(
          'fixed left-16 bottom-0 z-10 flex flex-col border-r border-surface-border bg-surface-base transition-[width] duration-200 ease-out motion-reduce:transition-none',
          isChatRoute ? 'top-0' : 'top-14',
          isOpen ? 'w-64' : 'w-0 overflow-hidden border-transparent pointer-events-none',
        )}
      >
        <div className="flex min-h-0 min-w-64 flex-1 flex-col">
          <div className="flex shrink-0 items-center gap-2 border-b border-surface-border px-2 py-3">
            <button
              type="button"
              onClick={() => setOpen(false)}
              aria-label="Collapse bots"
              aria-expanded={isOpen}
              aria-controls={panelId}
              title="Collapse sidebar"
              className="flex size-9 shrink-0 items-center justify-center rounded-md text-text-secondary transition-colors hover:bg-surface-overlay hover:text-text-body focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-primary"
            >
              <IconCollapseSessionPanel className="shrink-0" />
            </button>
            <NavLink
              to="/"
              className="flex min-h-9 min-w-0 flex-1 items-center justify-center rounded-md bg-brand-primary-dim px-2 text-label font-medium text-brand-primary-light transition-colors hover:bg-brand-primary hover:text-white"
              aria-label="New chat"
            >
              New chat
            </NavLink>
          </div>
          <div className="min-h-0 flex-1 overflow-y-auto scrollbar-thin px-2 py-2">
            <p className="px-2 pb-1 text-[10px] font-semibold uppercase tracking-caps text-text-disabled">
              Bots
            </p>
            {bots.length === 0 && (
              <p className="px-2 py-1 text-caption text-text-secondary">
                None yet. Connect an MCP from the chat composer.
              </p>
            )}
            <ul className="m-0 mb-3 list-none space-y-0.5 p-0">
              {bots.map((a) => (
                <li key={a.id}>
                  <button
                    type="button"
                    title={a.name}
                    onClick={() => void startBot(a)}
                    className="block w-full truncate rounded-md px-2 py-2 text-left text-label text-text-secondary transition-colors hover:bg-surface-overlay hover:text-text-body focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-primary"
                  >
                    {a.name}
                    <span className="ml-1 font-mono text-[10px] text-text-disabled">
                      {a.protocol_type}
                    </span>
                  </button>
                </li>
              ))}
            </ul>
            <p className="px-2 pb-1 text-[10px] font-semibold uppercase tracking-caps text-text-disabled">
              Sessions
            </p>
            {isLoading && (
              <p className="px-2 py-2 text-caption text-text-secondary">Loading…</p>
            )}
            {isError && (
              <p className="px-2 py-2 text-caption text-semantic-error">Could not load sessions.</p>
            )}
            {data?.items.length === 0 && !isLoading && (
              <p className="px-2 py-2 text-caption text-text-secondary">No sessions yet.</p>
            )}
            <ul className="m-0 list-none space-y-0.5 p-0">
              {data?.items.map((s) => (
                <li key={s.session_id}>
                  <NavLink
                    to={`/chat/${s.session_id}`}
                    title={s.title}
                    className={({ isActive }) =>
                      cn(
                        'block truncate rounded-md px-2 py-2 text-left text-label text-text-body transition-colors',
                        'hover:bg-surface-overlay focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-primary',
                        isActive
                          ? 'bg-surface-overlay text-brand-primary-light'
                          : 'text-text-secondary hover:text-text-body',
                      )
                    }
                  >
                    {s.title}
                  </NavLink>
                </li>
              ))}
            </ul>
          </div>
        </div>
      </aside>
    </>
  )
}
