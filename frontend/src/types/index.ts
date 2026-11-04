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

// ── Run audit (`GET /sessions/{id}/audit`, SuperAgent `api/models.py`) ─────────
// The server drops null fields (`response_model_exclude_none`), so anything
// nullable on the server is optional here. See frontend/TRUST-INDICATORS.md.

export interface RunAuditStep {
  seq: number
  agent_id: string
  capability_id?: string
  protocol?: string
  internal_tool_name?: string
  /**
   * Structural check (pipeline `_structural_verify`), not a gate check.
   * Absent when the step carries no structural verdict: unchecked.
   */
  verified?: boolean
  verdict_reason?: string
  base_fee?: string
  total_cost_usd?: string
}

export interface RunAuditSummary {
  total_steps: number
  /** Steps whose structural check passed; an unchecked step is never counted here. */
  steps_verified: number
  steps_failed: number
  /** Steps with no structural verdict. */
  steps_unchecked?: number
  protocols: string[]
  total_cost_usd: string
  duration_ms?: number
}

/** The settle gate's deciding row for the session's latest sealed run (keyed by run). */
export interface RunAuditGate {
  outcome: string
  failed_checks: string[]
  envelope_digest: string
  created_at: string
  /** False when the gate judged a run that charged nothing (verdict only, AD-12). */
  charged?: boolean
}

/**
 * Story 2.6: what a routine firing's sealed run amounts to in settlement.
 * Present only for a firing's session. Every field is server-built; the UI
 * renders `label` and `statement` and derives no words from `state`.
 */
export interface RunAuditSettlement {
  run_id: string
  state: FiringState
  /** e.g. "attested but unsettled", "settled — verdict only, nothing charged". */
  label: string
  gate_evaluated: boolean
  verdict_only?: boolean
  /** Gate ids from the deciding ledger row. */
  failed_checks?: string[]
  /** The signed `verdicts[]` entries that failed: `{check, detail?}`. */
  failed_verdicts?: Array<Record<string, string>>
  checks?: 'unchecked' | 'checked' | 'not_evaluated'
  checks_label?: string
  /** One sentence saying what was and was not decided for the run. */
  statement: string
}

/** The routine firing this session belongs to, as its row records it. */
export interface RunAuditFiring {
  routine_id: string
  state: FiringState
  label: string
  detail?: string
}

/** What the export and its signed receipt cover (FR-8, story 3.2). Server-written; render as is. */
export interface RunAuditCoverage {
  /** The verifier's coverage statement, computed from the envelope at export time. */
  statement: string
  run_id?: string
  receipt_steps?: number
  receipt_tools: string[]
  /** How this export's step list relates to the signed receipt. */
  export: string
}

export interface RunAuditResponse {
  session_id: string
  generated_at: string
  goal: string
  summary: RunAuditSummary
  steps: RunAuditStep[]
  note: string
  coverage: RunAuditCoverage
  /** The sealed run's signed ``model`` verdicts; absent when no receipt was read. */
  models?: string[]
  /** The session's latest sealed run: a firing's run, else the newest sealed envelope. */
  run_id?: string
  gate?: RunAuditGate
  /** Firing sessions only. */
  settlement?: RunAuditSettlement
  firing?: RunAuditFiring
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
  // Scope gate (story 1.5): a write or destructive call on a connection.
  scope_class?: 'read' | 'write' | 'destructive' | null
  connection_id?: string | null
  connection_name?: string | null
  target?: string | null
  call_id?: string | null
  capability_id?: string | null
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
      /** "<route>/<model id>" the server recorded for the turn (story 3.3). */
      model?: string
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
  // Routine fields (story 2.1); empty on a template saved from a chat.
  schedule_cron?: string | null
  schedule_tz?: string | null
  schedule_enabled?: boolean
  model?: string | null
  scope_allow?: string[]
  criteria?: Record<string, boolean>
  criteria_operands?: Record<string, Record<string, string | number | boolean>>
  // Story 2.2: when the schedule next fires, and how the last firing ended.
  next_run_at?: string | null
  last_firing?: FiringResponse | null
}

/** AD-22: the one firing state vocabulary every pane and export uses. */
export type FiringState =
  | 'scheduled'
  | 'running'
  | 'attested_unsettled'
  | 'settled'
  | 'refused'
  | 'paused'
  | 'skipped'
  | 'error'

export interface FiringResponse {
  id: string
  slot: string
  state: FiringState
  detail: string | null
  /** The firing's session: a paused firing's approval card waits there. */
  session_id: string | null
  run_id: string | null
  created_at: string
  updated_at: string
  // Story 2.5: every field below is computed by the server. The pane renders
  // them and never derives words from `state`.
  /** The state in words, e.g. "refused — counts_match"; read from the row. */
  label: string
  /** Error/skipped/paused detail, or the unsettled note (story 2.6). */
  note?: string | null
  /** AD-12: the deciding ledger row's kind; set only on settled/refused. */
  gate?: 'verdict_only' | 'charged' | null
  /** "verdict only, nothing charged" when the deciding row moved no money. */
  gate_label?: string | null
  /** The deciding ledger row's failed-check ids. */
  gate_checks?: string[]
  /** A sealed envelope is stored for this run, in the firing's session. */
  receipt_available?: boolean
  /** The viewer owns that session, so the receipt route lets them through. */
  receipt_downloadable?: boolean
  /** Declared criteria against the signed verdicts; null when no envelope was read. */
  checks?: 'unchecked' | 'checked' | 'not_evaluated' | null
  /** "recorded, unchecked" / "checked: …" / "declared, not evaluated: …". */
  checks_label?: string | null
}

export interface CreateRoutineRequest {
  name: string
  description?: string
  goal: string
  connections: string[]
  /** `<connection DID>#<capability>`; destructive capabilities are refused at save. */
  scope_allow: string[]
  model: string
  criteria: Record<string, boolean>
  criteria_operands: Record<string, Record<string, string | number | boolean>>
  cron: string
  timezone: string
}

/** A refused routine save: the field at fault and a reason naming it. */
export interface RoutineRejection {
  field: string
  reason: string
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
  /** The model that ran the turn, "<route>/<model id>", as the server recorded
   *  it (SSE ``done`` event, or the transcript row's ``tool_inputs.model``). */
  model?: string
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
