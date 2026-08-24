import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { NavBar } from '../components/layout/NavBar'
import { Sidebar } from '../components/layout/Sidebar'
import { SessionListPanel } from '../components/layout/SessionListPanel'
import { InputBar } from '../components/ui/InputBar'
import { ModelChip } from '../components/chat/ModelChip'
import { Logo } from '../components/ui/Logo'
import { cn } from '../components/ui/cn'
import { sessions } from '../api/client'
import { useSessionStore } from '../store/session'
import { useSessionSidebarStore } from '../store/sessionSidebar'
import { useAuthStore } from '../store/auth'
import { useSettingsStore } from '../store/settings'
import { useByokStore } from '../store/byok'
import { useSSE } from '../hooks/useSSE'
import { sessionTitleFromMessage } from '../lib/sessionTitle'
import { queryClient } from '../lib/queryClient'

const M2_DEMO_GOAL =
  'Show me my portfolio performance, use your web scraper agent to summarize https://en.wikipedia.org/wiki/Nvidia, and screenshot the Alpaca dashboard'

const SAMPLE_PROMPTS: { label: string; message: string }[] = [
  {
    label: 'Portfolio dashboard',
    message: 'Show me my portfolio performance and top holdings',
  },
  {
    label: '3-protocol demo (portfolio + scrape + screenshot)',
    message: M2_DEMO_GOAL,
  },
  {
    label: 'Wikipedia summary (web scraper)',
    message:
      'Summarize https://en.wikipedia.org/wiki/Artificial_intelligence with the web scraper agent',
  },
]

const SANDBOX_MODE = import.meta.env.VITE_SANDBOX_MODE === 'true'
const LOCAL_MODE = import.meta.env.VITE_LOCAL_MODE === 'true'
// Both modes offer a no-signup session; sandbox = throwaway guest, local = persistent user.
const AUTO_LOGIN = SANDBOX_MODE || LOCAL_MODE

export function Home() {
  const [input, setInput] = useState('')
  const [customInstructions, setCustomInstructions] = useState('')
  const [loading, setLoading] = useState(false)
  const [guestBootstrapping, setGuestBootstrapping] = useState(AUTO_LOGIN)
  const navigate = useNavigate()
  const isAuthenticated = useAuthStore((s) => s.isAuthenticated)
  const guestLogin = useAuthStore((s) => s.guestLogin)
  const localLogin = useAuthStore((s) => s.localLogin)
  const sessionSidebarOpen = useSessionSidebarStore((s) => s.isOpen)
  const defaultModel = useSettingsStore((s) => s.defaultModel)
  const { setSessionId, addMessage, reset } = useSessionStore()
  const { streamResponse } = useSSE()

  const autoLogin = SANDBOX_MODE ? guestLogin : localLogin

  useEffect(() => {
    if (!AUTO_LOGIN || isAuthenticated) {
      setGuestBootstrapping(false)
      return
    }
    let cancelled = false
    ;(async () => {
      try {
        await autoLogin()
      } catch {
        /* fall through to sign-in prompt */
      } finally {
        if (!cancelled) setGuestBootstrapping(false)
      }
    })()
    return () => {
      cancelled = true
    }
  }, [autoLogin, isAuthenticated])

  const handleSubmit = async (message: string, _artifactIds: string[] = []) => {
    if (!isAuthenticated) {
      if (AUTO_LOGIN) {
        try {
          await autoLogin()
        } catch {
          navigate('/login')
          return
        }
      } else {
        navigate('/login')
        return
      }
    }
    setLoading(true)
    try {
      reset()
      const title = sessionTitleFromMessage(message)
      const { session_id } = await sessions.create({ title })
      // Apply BYOK '__llm__' credentials to the new session (no-op when hosted).
      void useByokStore.getState().applyToSession(session_id)
      setSessionId(session_id)
      addMessage({
        id: crypto.randomUUID(),
        role: 'user',
        content: message,
        timestamp: Date.now(),
      })
      navigate(`/chat/${session_id}`)
      const byok = useByokStore.getState()
      const res = await sessions.sendMessage(session_id, message, [], {
        model:
          byok.mode === 'byok' && byok.model.trim()
            ? byok.model.trim()
            : defaultModel,
        customInstructions:
          byok.systemPrompt.trim() || customInstructions.trim() || undefined,
      })
      if (res.ok) await streamResponse(res)
      void queryClient.invalidateQueries({ queryKey: ['sessions'] })
      void queryClient.invalidateQueries({ queryKey: ['transcript', session_id] })
    } catch {
      // Error surfaced by fetch / SSE consumers if needed
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="min-h-screen bg-surface-canvas flex flex-col">
      <NavBar />
      <Sidebar />
      {isAuthenticated ? <SessionListPanel /> : null}

      {/* Glow background */}
      <div
        className="fixed pointer-events-none"
        style={{
          width: 700,
          height: 500,
          left: '50%',
          top: 100,
          transform: 'translateX(-50%)',
          background: 'radial-gradient(ellipse at center, rgba(59,110,248,0.07) 0%, rgba(9,9,11,0) 70%)',
          borderRadius: '50%',
        }}
        aria-hidden="true"
      />

      {/* Content */}
      <main
        className={cn(
          'flex flex-1 flex-col items-center justify-center px-4 pt-14 transition-[margin-left] duration-200 ease-out motion-reduce:transition-none',
          isAuthenticated && sessionSidebarOpen && 'ml-80',
          !isAuthenticated || !sessionSidebarOpen ? 'ml-16' : null,
        )}
      >
        {/* Logo badge */}
        <div className="size-16 flex items-center justify-center rounded-xl bg-brand-primary-dim border border-[rgba(59,110,248,0.35)] shadow-blue mb-6">
          <Logo size={36} />
        </div>

        {/* Headline */}
        <h1 className="font-display text-[38px] font-bold text-text-heading text-center leading-tight max-w-[760px] mb-3">
          The open harness for agent orchestration.
        </h1>
        <p className="font-mono text-[13px] text-text-secondary text-center max-w-[640px] mb-6">
          one goal in → verified multi-protocol run out → live dashboard, not a chat reply
        </p>
        <p className="text-body-lg text-text-secondary text-center max-w-[600px] mb-8">
          Type a goal. Orcha discovers the right agents, composes them across MCP, A2A, and COMPUTER_USE,
          and renders the result as a live dashboard — not a chat reply.
        </p>

        {/* Prompt input */}
        <div className="w-full max-w-[680px] mb-4">
          <div className="mb-2 flex">
            <ModelChip />
          </div>
          <InputBar
            value={input}
            onChange={setInput}
            onSubmit={handleSubmit}
            placeholder="describe a goal…"
            disabled={loading || guestBootstrapping}
            size="home"
          />
        </div>

        {/* Custom instructions */}
        <div className="w-full max-w-[680px] mb-6">
          <label
            htmlFor="custom-instructions"
            className="block text-[12px] font-medium text-text-secondary mb-1"
          >
            Custom instructions (optional)
          </label>
          <textarea
            id="custom-instructions"
            value={customInstructions}
            onChange={(e) => setCustomInstructions(e.target.value)}
            placeholder="Tell the harness how to behave for your work…"
            rows={2}
            maxLength={2000}
            disabled={loading || guestBootstrapping}
            className="w-full px-3 py-2 rounded-md bg-surface-elevated border border-surface-border text-label text-text-body placeholder:text-text-disabled focus:outline-none focus:border-brand-primary resize-y disabled:opacity-50"
          />
        </div>

        {/* Sample chips */}
        <div className="flex flex-wrap items-center justify-center gap-2.5 max-w-[760px]">
          {SAMPLE_PROMPTS.map((prompt) => (
            <button
              key={prompt.message}
              onClick={() => handleSubmit(prompt.message)}
              disabled={loading || guestBootstrapping}
              className="h-9 px-3 rounded-md bg-surface-overlay border border-surface-border text-[12px] text-text-secondary hover:text-text-body hover:border-surface-borderLight transition-colors duration-150 disabled:opacity-50"
            >
              {prompt.label}
            </button>
          ))}
        </div>

        {/* Goal-type hint + known limits */}
        <p className="mt-4 font-mono text-[11px] text-text-disabled text-center">
          works today: portfolio · web summaries · screenshots · email me the receipt
        </p>
        <p className="mt-1 font-mono text-[11px] text-text-disabled text-center">
          2 goals per guest · demo fleet · free-tier models
        </p>

        {SANDBOX_MODE && (
          <p className="mt-4 text-[12px] text-text-disabled text-center max-w-[560px]">
            Sandbox (Beta) uses pre-seeded demo agents. Portfolio numbers are illustrative — not
            connected to your accounts. Beta — you may hit session errors; we're actively hardening it.
          </p>
        )}

        {/* Agents footnote */}
        <p className="mt-6 text-[11px] text-text-disabled text-center">
          {'→ powered by '}
          <a href="/agents" className="underline underline-offset-2 hover:text-text-secondary transition-colors">
            live agents
          </a>
        </p>

        {/* Gate note */}
        {!isAuthenticated && (
          <p className="mt-3 text-[12px] text-text-disabled text-center">
            {SANDBOX_MODE && guestBootstrapping
              ? 'Starting guest demo session…'
              : SANDBOX_MODE
                ? 'Try a goal — no account needed'
                : 'Sign in required to start a session'}
          </p>
        )}
      </main>
    </div>
  )
}
