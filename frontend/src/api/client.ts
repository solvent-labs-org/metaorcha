import type {
  ArtifactResponse,
  AuthResponse,
  CredentialPayload,
  HealthResponse,
  ListAgentsResponse,
  RegisterAgentResponse,
  SessionStatusResponse,
  UserSettings,
  WalletBalanceResponse,
  WalletFundResponse,
  WalletTransactionsResponse,
  WithdrawRequest,
  WorkflowResponse,
  WorkflowStatus,
} from '../types'
import type { PaginatedSessionsDTO, TranscriptEntryDTO } from '../types/transcript'

const BASE_URL = import.meta.env.VITE_GATEWAY_URL ?? 'http://localhost:8080'

// ── Token storage ─────────────────────────────────────────────────────────────
let _accessToken: string | null = localStorage.getItem('access_token')

export function setAccessToken(token: string | null) {
  _accessToken = token
  if (token) localStorage.setItem('access_token', token)
  else localStorage.removeItem('access_token')
}

export function getAccessToken(): string | null {
  return _accessToken
}

// ── Core fetch wrapper ────────────────────────────────────────────────────────
async function apiFetch<T>(
  path: string,
  options: RequestInit = {},
  retry = true,
): Promise<T> {
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    ...(options.headers as Record<string, string>),
  }

  if (_accessToken) {
    headers['Authorization'] = `Bearer ${_accessToken}`
  }

  const res = await fetch(`${BASE_URL}${path}`, { ...options, headers })

  if (res.status === 401 && retry) {
    const refreshed = await tryRefresh()
    if (refreshed) return apiFetch<T>(path, options, false)
    throw new Error('Unauthorized')
  }

  if (!res.ok) {
    const text = await res.text().catch(() => res.statusText)
    throw new Error(`${res.status}: ${text}`)
  }

  if (res.status === 204) return undefined as T
  return res.json() as Promise<T>
}

/** Like apiFetch but returns raw Response (for SSE streaming). Retries once on 401. */
async function streamFetch(path: string, options: RequestInit, retry = true): Promise<Response> {
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    ...(options.headers as Record<string, string>),
  }
  if (_accessToken) headers['Authorization'] = `Bearer ${_accessToken}`
  const res = await fetch(`${BASE_URL}${path}`, { ...options, headers })
  if (res.status === 401 && retry) {
    const refreshed = await tryRefresh()
    if (refreshed) return streamFetch(path, options, false)
  }
  return res
}

export async function tryRefresh(): Promise<boolean> {
  const refreshToken = localStorage.getItem('refresh_token')
  if (!refreshToken) return false
  try {
    const res = await fetch(`${BASE_URL}/auth/refresh`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ refresh_token: refreshToken }),
    })
    if (!res.ok) return false
    const data: AuthResponse = await res.json()
    setAccessToken(data.access_token)
    localStorage.setItem('refresh_token', data.refresh_token)
    return true
  } catch {
    return false
  }
}

// ── Auth ──────────────────────────────────────────────────────────────────────
export const auth = {
  register: (email: string, password: string, display_name?: string) =>
    apiFetch<AuthResponse>('/auth/register', {
      method: 'POST',
      body: JSON.stringify({ email, password, display_name }),
    }),

  login: (email: string, password: string) =>
    apiFetch<AuthResponse>('/auth/login', {
      method: 'POST',
      body: JSON.stringify({ email, password }),
    }),

  /** Hosted sandbox: no signup — one guest message per token (SANDBOX_MODE). */
  guest: () =>
    apiFetch<AuthResponse>('/auth/guest', { method: 'GET' }),

  /** Self-hosted local: persistent single-user session, no signup (LOCAL_MODE). */
  local: () =>
    apiFetch<AuthResponse>('/auth/local', { method: 'GET' }),

  logout: () =>
    apiFetch<void>('/auth/logout', { method: 'POST' }),
}

// ── Sessions ──────────────────────────────────────────────────────────────────
export const sessions = {
  create: (opts?: { title?: string }) =>
    apiFetch<{ session_id: string }>('/api/v1/sessions', {
      method: 'POST',
      body: JSON.stringify({ title: opts?.title ?? null }),
    }),

  list: (opts?: { page?: number; page_size?: number }) => {
    const qs = new URLSearchParams()
    if (opts?.page != null) qs.set('page', String(opts.page))
    if (opts?.page_size != null) qs.set('page_size', String(opts.page_size))
    const q = qs.toString()
    return apiFetch<PaginatedSessionsDTO>(
      `/api/v1/sessions${q ? `?${q}` : ''}`,
    )
  },

  getTranscript: (sessionId: string) =>
    apiFetch<{ entries: TranscriptEntryDTO[] }>(
      `/api/v1/sessions/${sessionId}/transcript`,
    ),

  getAudit: (sessionId: string) =>
    apiFetch<Record<string, unknown>>(`/api/v1/sessions/${sessionId}/audit`),

  getStatus: (sessionId: string) =>
    apiFetch<SessionStatusResponse>(`/api/v1/sessions/${sessionId}/status`),

  /** Returns raw Response — caller streams the body */
  sendMessage: (
    sessionId: string,
    message: string,
    artifactIds: string[] = [],
    options: { model?: string; customInstructions?: string } = {},
  ): Promise<Response> =>
    streamFetch(`/api/v1/sessions/${sessionId}/message`, {
      method: 'POST',
      body: JSON.stringify({
        message,
        artifact_ids: artifactIds,
        ...(options.model ? { model: options.model } : {}),
        ...(options.customInstructions
          ? { custom_instructions: options.customInstructions }
          : {}),
      }),
    }),

  /**
   * Resume an interrupted session.
   * `interrupt_type` and `interrupt_id` are from the InterruptEvent.
   * `value` is the resume payload dict — forwarded verbatim to the suspended interrupt() call.
   */
  resume: (
    sessionId: string,
    interrupt_id: string,
    interrupt_type: string,
    value: Record<string, unknown>,
  ): Promise<Response> =>
    streamFetch(`/api/v1/sessions/${sessionId}/resume`, {
      method: 'POST',
      body: JSON.stringify({ interrupt_id, interrupt_type, value }),
    }),

  stop: (sessionId: string) =>
    apiFetch<{ ok: boolean; status: 'stopping' | 'not_running' }>(
      `/api/v1/sessions/${sessionId}/stop`,
      { method: 'POST' },
    ),
}

// ── Files ─────────────────────────────────────────────────────────────────────
export const files = {
  upload: async (file: File, sessionId?: string): Promise<ArtifactResponse> => {
    const form = new FormData()
    form.append('file', file)
    if (sessionId) form.append('session_id', sessionId)
    const headers: Record<string, string> = {}
    if (_accessToken) headers['Authorization'] = `Bearer ${_accessToken}`
    const res = await fetch(`${BASE_URL}/api/v1/files/upload`, {
      method: 'POST',
      headers,
      body: form,
    })
    if (!res.ok) {
      const text = await res.text().catch(() => res.statusText)
      throw new Error(`${res.status}: ${text}`)
    }
    return res.json() as Promise<ArtifactResponse>
  },

  download: (artifactId: string): string =>
    `${BASE_URL}/api/v1/files/${artifactId}/download`,

  listForSession: (sessionId: string) =>
    apiFetch<
      Array<{
        artifact_id: string
        filename: string
        mime_type: string
        size_bytes: number
        source: string
        created_at: string
      }>
    >(`/api/v1/sessions/${sessionId}/artifacts`),
}

export interface AgentSecretItem {
  agent_id: string
  agent_name: string
  var_name: string
  description: string | null
  required: boolean
}

// ── Credentials ───────────────────────────────────────────────────────────────
export const credentials = {
  set: (payload: CredentialPayload) =>
    apiFetch<void>('/api/v1/credentials', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  delete: (payload: Omit<CredentialPayload, 'value'>) =>
    apiFetch<void>('/api/v1/credentials', {
      method: 'DELETE',
      body: JSON.stringify(payload),
    }),

  getRequired: (agentIds: string[]) => {
    const qs = agentIds.map((id) => `agent_ids=${encodeURIComponent(id)}`).join('&')
    return apiFetch<AgentSecretItem[]>(`/api/v1/credentials/required?${qs}`)
  },
}

// ── Workflows ─────────────────────────────────────────────────────────────────
export const workflows = {
  list: () =>
    apiFetch<WorkflowResponse[]>('/api/v1/workflows'),

  get: (id: string) =>
    apiFetch<WorkflowResponse>(`/api/v1/workflows/${id}`),

  create: (session_id: string, name: string, description?: string) =>
    apiFetch<WorkflowResponse>('/api/v1/workflows', {
      method: 'POST',
      body: JSON.stringify({ session_id, name, description }),
    }),

  update: (id: string, patch: { name?: string; description?: string; status?: WorkflowStatus }) =>
    apiFetch<WorkflowResponse>(`/api/v1/workflows/${id}`, {
      method: 'PATCH',
      body: JSON.stringify(patch),
    }),

  delete: (id: string) =>
    apiFetch<void>(`/api/v1/workflows/${id}`, { method: 'DELETE' }),
}

// ── Dev Agents ────────────────────────────────────────────────────────────────
export const devAgents = {
  list: (params: { page?: number; limit?: number; status?: string; protocol?: string } = {}) => {
    const qs = new URLSearchParams()
    if (params.page) qs.set('page', String(params.page))
    if (params.limit) qs.set('limit', String(params.limit))
    if (params.status) qs.set('status', params.status)
    if (params.protocol) qs.set('protocol', params.protocol)
    return apiFetch<ListAgentsResponse>(`/api/v1/dev/agents?${qs}`)
  },

  register: async (file: File): Promise<RegisterAgentResponse> => {
    const form = new FormData()
    form.append('emerge_yaml', file)
    const headers: Record<string, string> = {}
    if (_accessToken) headers['Authorization'] = `Bearer ${_accessToken}`
    const res = await fetch(`${BASE_URL}/api/v1/dev/agents`, {
      method: 'POST',
      headers,
      body: form,
    })
    if (!res.ok) {
      const text = await res.text().catch(() => res.statusText)
      throw new Error(`${res.status}: ${text}`)
    }
    return res.json() as Promise<RegisterAgentResponse>
  },

  get: (id: string) =>
    apiFetch<Record<string, unknown>>(`/api/v1/dev/agents/${id}`),

  delete: (id: string) =>
    apiFetch<void>(`/api/v1/dev/agents/${id}`, { method: 'DELETE' }),
}

// ── Settings ──────────────────────────────────────────────────────────────────
export const settingsApi = {
  get: () =>
    apiFetch<UserSettings>('/api/v1/settings/me'),

  update: (patch: { display_name?: string; is_dev_mode?: boolean }) =>
    apiFetch<UserSettings>('/api/v1/settings/me', {
      method: 'PATCH',
      body: JSON.stringify(patch),
    }),
}

// ── Wallet ────────────────────────────────────────────────────────────────────
export const walletApi = {
  getBalance: () =>
    apiFetch<WalletBalanceResponse>('/wallet/balance'),

  getFundInfo: () =>
    apiFetch<WalletFundResponse>('/wallet/fund'),

  getTransactions: (page = 1) =>
    apiFetch<WalletTransactionsResponse>(`/wallet/transactions?page=${page}`),

  withdraw: (body: WithdrawRequest) =>
    apiFetch<{ action_id: string; status: string }>('/wallet/withdraw', {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  getTransferStatus: (actionId: string) =>
    apiFetch<{ action_id: string; status: string }>(`/wallet/transfer/${actionId}/status`),
}

// ── Health ────────────────────────────────────────────────────────────────────
export const health = {
  check: () => apiFetch<HealthResponse>('/health'),
}
