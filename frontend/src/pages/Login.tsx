import { useState } from 'react'
import { useNavigate, Link } from 'react-router-dom'
import { Button } from '../components/ui/Button'
import { Logo } from '../components/ui/Logo'
import { useAuthStore } from '../store/auth'

const SANDBOX_MODE = import.meta.env.VITE_SANDBOX_MODE === 'true'
const LOCAL_MODE = import.meta.env.VITE_LOCAL_MODE === 'true'
const AUTO_LOGIN = SANDBOX_MODE || LOCAL_MODE

export function Login() {
  const navigate = useNavigate()
  const { login, register, guestLogin, localLogin } = useAuthStore()

  const [mode, setMode] = useState<'login' | 'register'>('login')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [displayName, setDisplayName] = useState('')
  const [loading, setLoading] = useState(false)
  const [guestLoading, setGuestLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const handleSubmit = async () => {
    if (!email || !password) return
    setLoading(true)
    setError(null)
    try {
      if (mode === 'login') {
        await login(email, password)
      } else {
        await register(email, password, displayName || undefined)
      }
      navigate('/')
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Authentication failed')
    } finally {
      setLoading(false)
    }
  }

  const handleGuest = async () => {
    setGuestLoading(true)
    setError(null)
    try {
      await (SANDBOX_MODE ? guestLogin() : localLogin())
      navigate('/')
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not start a session')
    } finally {
      setGuestLoading(false)
    }
  }

  return (
    <div className="min-h-screen bg-surface-canvas flex items-center justify-center px-4">
      <div className="w-full max-w-sm">
        {/* Logo */}
        <div className="flex flex-col items-center mb-8">
          <div className="size-14 flex items-center justify-center rounded-xl bg-brand-primary-dim border border-[rgba(59,110,248,0.35)] mb-3">
            <Logo size={34} />
          </div>
          <h1 className="text-h3 text-text-heading">Orcha</h1>
          <p className="text-caption text-text-secondary mt-1">
            {mode === 'login' ? 'Sign in to your account' : 'Create a new account'}
          </p>
        </div>

        {/* Form */}
        <div className="bg-surface-elevated border border-surface-border rounded-lg p-6 flex flex-col gap-4">
          {mode === 'register' && (
            <div className="flex flex-col gap-1">
              <label htmlFor="display-name" className="text-caption font-medium text-text-secondary">
                Display Name
              </label>
              <input
                id="display-name"
                value={displayName}
                onChange={(e) => setDisplayName(e.target.value)}
                placeholder="Your name"
                className="h-10 px-3 rounded-md bg-surface-base border border-surface-borderLight text-label text-text-body placeholder:text-text-disabled focus:outline-none focus:border-brand-primary"
              />
            </div>
          )}

          <div className="flex flex-col gap-1">
            <label htmlFor="email" className="text-caption font-medium text-text-secondary">Email</label>
            <input
              id="email"
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              placeholder="you@example.com"
              className="h-10 px-3 rounded-md bg-surface-base border border-surface-borderLight text-label text-text-body placeholder:text-text-disabled focus:outline-none focus:border-brand-primary"
            />
          </div>

          <div className="flex flex-col gap-1">
            <label htmlFor="password" className="text-caption font-medium text-text-secondary">Password</label>
            <input
              id="password"
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && handleSubmit()}
              placeholder="Minimum 8 characters"
              className="h-10 px-3 rounded-md bg-surface-base border border-surface-borderLight text-label text-text-body placeholder:text-text-disabled focus:outline-none focus:border-brand-primary"
            />
          </div>

          {error && (
            <p className="text-caption text-semantic-error" role="alert">{error}</p>
          )}

          <Button onClick={handleSubmit} loading={loading} className="w-full h-11">
            {mode === 'login' ? 'Sign In' : 'Create Account'}
          </Button>

          <p className="text-center text-caption text-text-secondary">
            {mode === 'login' ? (
              <>Don't have an account?{' '}
                <button onClick={() => setMode('register')} className="text-brand-primary hover:underline">
                  Register
                </button>
              </>
            ) : (
              <>Already have an account?{' '}
                <button onClick={() => setMode('login')} className="text-brand-primary hover:underline">
                  Sign In
                </button>
              </>
            )}
          </p>

          {AUTO_LOGIN && (
            <>
              <div className="flex items-center gap-3">
                <div className="h-px flex-1 bg-surface-border" />
                <span className="text-caption text-text-disabled">or</span>
                <div className="h-px flex-1 bg-surface-border" />
              </div>
              <Button
                variant="secondary"
                onClick={handleGuest}
                loading={guestLoading}
                className="w-full h-11"
              >
                {SANDBOX_MODE ? 'Continue as guest' : 'Continue without an account'}
              </Button>
            </>
          )}
        </div>

        <p className="mt-4 text-center text-caption text-text-disabled">
          <Link to="/" className="hover:text-text-secondary transition-colors">← Back to Home</Link>
        </p>
      </div>
    </div>
  )
}
