// ── Auth ──────────────────────────────────────────────────────────────────────
export interface AuthResponse {
  access_token: string
  refresh_token: string
  token_type: 'bearer'
}

// ── Sessions ──────────────────────────────────────────────────────────────────
/** Optional server-driven log lines; merged additively when present. */
export interface SessionStatusActivityItem {
  id?: string
  message?: string
  text?: string
  timestamp?: number | string
  at?: string
  level?: string
}

export interface SessionStatusResponse {
  session_id: string
  status: 'ready' | 'interrupted' | 'not_found'
  /** Full InterruptEvent when session is interrupted; replaces legacy pending_interrupt. */
  active_interrupt: InterruptEvent | null
  estimated_token_count: number
  artifacts: Record<string, unknown>
  pnd_candidates: unknown[]
  task_checklist: unknown | null
  captured_workflow: unknown | null
  activity_log?: SessionStatusActivityItem[]
  session_logs?: SessionStatusActivityItem[]
}

/** Session Details — activity log (API + client append). */
export interface SessionLogEntry {
  id: string
  timestamp: number
  message: string
}

// ── Interrupt types (manually synced with internal_commons.interrupts) ─────────

export type InterruptType =
  | 'AUTH_CALLBACK'
  | 'AUTH_FORM_SUBMISSION'
  | 'AGENT_OAUTH_CALLBACK'
  | 'HITL_APPROVAL'
  | 'HITL_CLARIFICATION'
  | 'AGENT_CLARIFICATION'
  | 'CRM_SETUP'
  | 'INSUFFICIENT_CREDITS'

export interface AuthCallbackMetadata {
  auth_url: string
  provider_name: string
  scopes: string[]
}

export interface AuthFormSubmissionMetadata {
  form_title: string
  field_label: string
  vault_key: string
  is_secret: boolean
  agent_display_name?: string | null
}

export interface AgentOAuthCallbackMetadata {
  auth_url: string
  provider_name: string
  scopes: string[]
  agent_id: string
}

export interface HitlApprovalMetadata {
  action_description: string
  risk_level: 'low' | 'medium' | 'high'
  agent_display_name: string
  capability_name: string
}

export interface HitlClarificationMetadata {
  question: string
}

export interface AgentClarificationMetadata {
  question: string
  agent_display_name: string
  agent_id: string
}

export interface CrmSetupMetadata {
  tenant_id: string
  agent_display_name: string
  lead_gen_url: string
}

export interface InsufficientCreditsMetadata {
  reason: 'insufficient_credits' | 'arrears' | 'user_not_found'
  amount_owed: string
  agent_display_name: string
}

export type InterruptMetadata =
  | AuthCallbackMetadata
  | AuthFormSubmissionMetadata
  | AgentOAuthCallbackMetadata
  | HitlApprovalMetadata
  | HitlClarificationMetadata
  | AgentClarificationMetadata
  | CrmSetupMetadata
  | InsufficientCreditsMetadata

export interface InterruptEvent {
  type: 'interrupt'
  interrupt_type: InterruptType
  interrupt_id: string
  agent_id: string
  session_id: string
  message: string
  metadata: Record<string, unknown>
  resumable: boolean
}

// ── SSE Events (type-based — SuperAgent passthrough, no gateway transformation) ─

export type ChecklistTaskStatus = 'pending' | 'running' | 'done' | 'failed'

export interface ChecklistTask {
  id: string
  label: string
  status: ChecklistTaskStatus
}

export interface AgentInfo {
  agent_id: string
  name: string
  type: 'mcp' | 'a2a' | 'native'
  status: 'running' | 'done' | 'pending'
}

/** Live tool / agent invocation row (SSE + session store). */
export type ToolInvocationPhase = 'running' | 'success' | 'error'

export interface ToolInvocationTrace {
  call_id: string
  tool_name: string
  agent_id: string
  phase: ToolInvocationPhase
  inputs: Record<string, unknown>
  progressLines: string[]
  content_preview: string
  /** Protocol used by this invocation: mcp | a2a | computer_use | acp */
  protocol?: string
  /** Monotonic per session — used to interleave with agent messages */
  sortIndex?: number
  /** Client clock when invocation.start arrived */
  startedAt?: number
  /** Total cost charged for this call: agent base_fee + LLM token cost. */
  total_cost_usd?: string
  /** Agent base_fee component. */
  base_fee?: string
  /** Whether the structural verifier passed for this step. */
  verified?: boolean
  /** Short reason string from the structural verifier. */
  verdict_reason?: string
  /** Current verifier retry attempt (1-based) while a transient step is being re-run. */
  retryAttempt?: number
  /** Total attempts allowed (1 + verify_max_retries). */
  maxAttempts?: number
}

/**
 * Raw SSE event shapes from SuperAgent (no gateway transformation layer).
 * Field names match SuperAgent's runner.py _extract_events() output exactly.
 */
export type SSEEvent =
  | { type: 'token'; content: string }
  | {
      type: 'invocation_start'
      call_id: string
      tool_name: string
      agent_id: string
      inputs: Record<string, unknown>
      capability_id?: string
      protocol?: string
    }
  | { type: 'invocation_progress'; call_id: string; status: string; message: string }
  | {
      type: 'invocation_retry'
      call_id: string
      tool_name: string
      agent_id: string
      /** 1-based attempt that just failed and is being retried. */
      attempt: number
      /** Total attempts allowed (1 + verify_max_retries). */
      max_attempts: number
      /** Transient failure category from the error taxonomy. */
      reason?: string
    }
  | {
      type: 'invocation_result'
      call_id: string
      tool_name: string
      agent_id: string
      status: string
      content_preview: string
      /** Total cost charged: base_fee + LLM token cost. Only present for paid A2A agents. */
      total_cost_usd?: string
      /** Agent base_fee component of total_cost_usd. */
      base_fee?: string
      /** Whether the structural verifier passed. */
      verified?: boolean
      /** Short reason from the structural verifier. */
      verdict_reason?: string
    }
  | {
      type: 'agents_discovered'
      agents: Array<{ agent_id: string; agent_name: string; protocol_type: string }>
    }
  | { type: 'token_usage'; estimated_token_count: number }
  | {
      type: 'artifact_created'
      artifact_id: string
      /** Legacy SSE field; prefer ``filename``. */
      description?: string
      filename?: string
      mime_type: string
      size_bytes: number
    }
  | InterruptEvent
  | {
      type: 'done'
      session_id: string
      run_id?: string
      attestation_path?: string
    }
  | { type: 'stopped'; session_id: string }
  | {
      type: 'checklist_snapshot'
      checklist_id: string
      goal: string
      version: number
      steps: Array<{ step_id: string; description: string; status: string }>
    }
  | { type: 'error'; error: string }
  | { type: 'auth_complete'; interrupt_type: string; message?: string }
  | { type: 'canvas_manifest'; manifest_id: string; manifest: import('./canvas').UIManifest; title?: string }

// ── Workflows ─────────────────────────────────────────────────────────────────
export type WorkflowStatus = 'active' | 'inactive' | 'scheduled'

export interface WorkflowResponse {
  id: string
  name: string
  description: string | null
  goal_template: string | null
  status: WorkflowStatus
  agents_used: string[]
  steps: unknown[]
  run_count: number
  created_at: string
  updated_at: string
}

// ── Dev Agents ────────────────────────────────────────────────────────────────
export interface AgentListItem {
  id: string
  name: string
  version: string
  health_status: 'healthy' | 'unhealthy' | 'unknown'
  protocol_type: 'mcp' | 'a2a'
  indexed_at: string
}

export interface ListAgentsResponse {
  status: 'success'
  data: {
    agents: AgentListItem[]
    pagination: {
      page: number
      limit: number
      total: number
      total_pages: number
    }
  }
}

export interface RegisterAgentResponse {
  status: 'success'
  data: {
    agent_id: string
    name: string
    version: string
    registered_at: string
    health_status: string
    capabilities_harvested: { tools: number; resources: number; prompts: number }
  }
}

// ── Settings ──────────────────────────────────────────────────────────────────
export interface UserSettings {
  user_id: string
  email: string
  display_name: string | null
  is_dev_mode: boolean
  credits_usd: string
}

// ── Wallet ────────────────────────────────────────────────────────────────────
export interface WalletBalanceResponse {
  credits_usd: number
  arrears_usd: number
  arrears_flag: boolean
  wallet_address: string | null
  chain: string
  on_chain_usdc: { total?: { value: string; currency: string }; assets?: unknown[] } | null
}

export interface WalletFundResponse {
  wallet_address: string | null
  chain: string
  asset: string
  note: string
  mock_mode?: boolean
}

export interface WalletTransaction {
  id: string
  session_id: string
  agent_id: string
  base_fee: number
  platform_cut: number
  developer_payout: number
  status: 'PENDING' | 'SETTLED' | 'FAILED'
  created_at: string
  settled_at: string | null
  tx_hash?: string | null
}

export interface WalletTransactionsResponse {
  transactions: WalletTransaction[]
  page: number
  total: number
}

export interface WithdrawRequest {
  amount: number
  to_address?: string
}

// ── Credentials ───────────────────────────────────────────────────────────────
export interface CredentialPayload {
  agent_id: string
  var_name: string
  value: string
  scope: 'permanent' | 'session'
  session_id?: string
}

// ── Health ────────────────────────────────────────────────────────────────────
export interface HealthResponse {
  status: 'ok' | 'degraded'
  services: { superagent: string; registry: string; redis: string }
}

// ── UI / Store types ──────────────────────────────────────────────────────────
export type MessageRole = 'user' | 'agent' | 'system' | 'error'

/** Files attached to a user message (persisted on USER transcript rows). */
export interface AttachedArtifactRef {
  artifact_id: string
  filename: string
  mime_type: string
  size_bytes: number
}

export interface ChatMessage {
  id: string
  role: MessageRole
  content: string
  streaming?: boolean
  /** True when this message was the orchestrator's ReAct reasoning that was
   *  interrupted by a tool call — rendered as a collapsible "Thinking" block. */
  streamedAsThinking?: boolean
  timestamp: number
  /** Monotonic per session — interleaves with tool cards */
  sortIndex?: number
  /** Shown in timeline for uploads; restored from transcript ``tool_inputs``. */
  attachedArtifacts?: AttachedArtifactRef[]
  /** Sealed-run id from the SSE ``done`` event when attestation is on. */
  runId?: string
  /** SuperAgent-relative fetch path; Gateway prefixes ``/api/v1``. */
  attestationPath?: string
}

export interface Artifact {
  artifact_id: string
  name: string
  type: string
}

export interface ArtifactResponse {
  artifact_id: string
  filename: string
  mime_type: string
  size_bytes: number
  session_id: string | null
}

export interface PendingArtifact extends ArtifactResponse {
  uploading: boolean
  error?: string
}

export interface Interrupt {
  interrupt_id: string
  interrupt_type: InterruptType
  message: string
  metadata: Record<string, unknown>
}

export type SessionStatus = 'idle' | 'running' | 'interrupted' | 'complete' | 'failed'
