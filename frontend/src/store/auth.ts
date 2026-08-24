import { create } from 'zustand'
import { auth, setAccessToken } from '../api/client'

interface AuthState {
  isAuthenticated: boolean
  email: string | null
  login: (email: string, password: string) => Promise<void>
  register: (email: string, password: string, displayName?: string) => Promise<void>
  guestLogin: () => Promise<void>
  localLogin: () => Promise<void>
  logout: () => Promise<void>
  initFromStorage: () => void
}

export const useAuthStore = create<AuthState>((set) => ({
  isAuthenticated: false,
  email: null,

  initFromStorage: () => {
    const hasToken = !!localStorage.getItem('access_token')
    if (hasToken) set({ isAuthenticated: true })
  },

  login: async (email, password) => {
    const data = await auth.login(email, password)
    setAccessToken(data.access_token)
    localStorage.setItem('refresh_token', data.refresh_token)
    set({ isAuthenticated: true, email })
  },

  register: async (email, password, displayName) => {
    const data = await auth.register(email, password, displayName)
    setAccessToken(data.access_token)
    localStorage.setItem('refresh_token', data.refresh_token)
    set({ isAuthenticated: true, email })
  },

  guestLogin: async () => {
    const data = await auth.guest()
    setAccessToken(data.access_token)
    set({ isAuthenticated: true, email: 'guest' })
  },

  localLogin: async () => {
    const data = await auth.local()
    setAccessToken(data.access_token)
    // Persistent session (unlike guest) — keep the refresh token across restarts.
    if (data.refresh_token) localStorage.setItem('refresh_token', data.refresh_token)
    set({ isAuthenticated: true, email: 'local' })
  },

  logout: async () => {
    try { await auth.logout() } catch { /* ignore */ }
    setAccessToken(null)
    localStorage.removeItem('refresh_token')
    set({ isAuthenticated: false, email: null })
  },
}))
