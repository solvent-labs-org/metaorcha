import { create } from 'zustand'
import type {
  AgentInfo,
  Artifact,
  AttachedArtifactRef,
  ChatMessage,
  ChecklistTask,
  Interrupt,
  SessionLogEntry,
  SessionStatus,
  SessionStatusResponse,
  ToolInvocationPhase,
  ToolInvocationTrace,
} from '../types'
import type { CanvasEntry } from '../types/canvas'
import type { TranscriptEntryDTO } from '../types/transcript'
import { coerceTokenCount } from '../lib/coerceTokenCount'
import { reduceSessionDetailsFromStatus } from '../lib/sessionDetailsReducer'

interface TokenUsage {
  input: number
  output: number
  total: number
}

export type RunPhase = 'planning' | 'executing' | null

/** One entry in the developer Trace tab — event type + arrival time. */
export interface TraceEvent {
  id: string
  type: string
  timestamp: number
}

/** Ring-buffer size for the Trace tab (developer mode). */
const TRACE_BUFFER_MAX = 100

interface SessionState {
  sessionId: string | null
  sessionName: string | null
  status: SessionStatus
  runPhase: RunPhase
  activeAgentName: string | null
  messages: ChatMessage[]
  agents: AgentInfo[]
  checklist: ChecklistTask[]
  artifacts: Artifact[]
  interrupts: Interrupt[]
  sessionLogs: SessionLogEntry[]
  tokenUsage: TokenUsage
  elapsedSeconds: number
  streamingMessageId: string | null
  credentialsModalOpen: boolean
  /** Session id for which BYOK model credentials were saved (null = env default). */
  byokSessionId: string | null
  saveWorkflowModalOpen: boolean
  crmModalOpen: boolean
  integrationsModalOpen: boolean
  toolTrace: ToolInvocationTrace[]
  canvases: CanvasEntry[]
  /** Last ~100 SSE event types + timestamps (developer Trace tab). */
  traceBuffer: TraceEvent[]
  /** Increments for each timeline-visible row (messages + tools) */
  timelineSeq: number

  // Actions
  setSessionId: (id: string) => void
  setSessionName: (name: string | null) => void
  setStatus: (status: SessionStatus) => void
  addMessage: (msg: ChatMessage) => void
  appendToken: (token: string) => void
  beginStreaming: () => string
  endStreaming: (markAsThinking?: boolean) => void
  attachReceiptToLastTurn: (receipt: {
    runId: string
    attestationPath: string
  }) => void
  setAgents: (agents: AgentInfo[]) => void
  updateAgent: (agentId: string, patch: Partial<AgentInfo>) => void
  setChecklist: (tasks: ChecklistTask[]) => void
  updateChecklist: (tasks: ChecklistTask[]) => void
  setArtifacts: (artifacts: Artifact[]) => void
  addArtifact: (artifact: Artifact) => void
  addInterrupt: (interrupt: Interrupt) => void
  appendSessionLog: (message: string) => void
  setTokenUsage: (usage: TokenUsage) => void
  mergeTokenUsage: (usage: { input?: unknown; output?: unknown; total?: unknown }) => void
  setElapsed: (seconds: number) => void
  openCredentialsModal: () => void
  closeCredentialsModal: () => void
  setByokActive: (sessionId: string) => void
  clearByokActive: () => void
  openSaveWorkflowModal: () => void
  closeSaveWorkflowModal: () => void
  openCrmModal: () => void
  closeCrmModal: () => void
  openIntegrationsModal: () => void
  closeIntegrationsModal: () => void
  /** Apply GET /sessions/:id/status — refreshes Session Details panel. */
  hydrateFromSessionStatus: (data: SessionStatusResponse) => void
  /** Replace messages + tool trace from persisted transcript (server order). */
  hydrateFromTranscript: (sessionId: string, entries: TranscriptEntryDTO[]) => void
  toolTraceStart: (p: {
    call_id: string
    tool_name: string
    agent_id: string
    inputs?: Record<string, unknown>
    protocol?: string
  }) => void
  toolTraceProgress: (callId: string, line: string) => void
  toolTraceRetry: (p: {
    call_id: string
    attempt: number
    max_attempts: number
    reason?: string
  }) => void
  toolTraceFinalize: (p: {
    call_id: string
    tool_name: string
    agent_id: string
    status: string
    content_preview: string
    total_cost_usd?: string
    base_fee?: string
    verified?: boolean
    verdict_reason?: string
  }) => void
  clearToolTrace: () => void
  addCanvas: (entry: CanvasEntry) => void
  /** Append an SSE event to the developer trace ring buffer. */
  recordTraceEvent: (type: string) => void
  reset: () => void
}

const initialState = {
  sessionId: null,
  sessionName: null,
  status: 'idle' as SessionStatus,
  runPhase: null as RunPhase,
  activeAgentName: null as string | null,
  messages: [],
  agents: [],
  checklist: [],
  artifacts: [],
  interrupts: [],
  sessionLogs: [],
  tokenUsage: { input: 0, output: 0, total: 0 },
  elapsedSeconds: 0,
  streamingMessageId: null,
  credentialsModalOpen: false,
  byokSessionId: null as string | null,
  saveWorkflowModalOpen: false,
  crmModalOpen: false,
  integrationsModalOpen: false,
  toolTrace: [] as ToolInvocationTrace[],
  canvases: [] as CanvasEntry[],
  traceBuffer: [] as TraceEvent[],
  timelineSeq: 0,
}

export const useSessionStore = create<SessionState>((set, get) => ({
  ...initialState,

  setSessionId: (id) => set({ sessionId: id }),
  setSessionName: (name) => set({ sessionName: name }),
  setStatus: (status) =>
    set((s) => ({
      status,
      runPhase: status === 'running' ? (s.runPhase ?? 'planning') : null,
      activeAgentName: status === 'running' ? s.activeAgentName : null,
    })),

  addMessage: (msg) =>
    set((s) => {
      const seq = s.timelineSeq + 1
      return {
        timelineSeq: seq,
        messages: [...s.messages, { ...msg, sortIndex: msg.sortIndex ?? seq }],
      }
    }),

  beginStreaming: () => {
    const id = crypto.randomUUID()
    set((s) => {
      const seq = s.timelineSeq + 1
      const msg: ChatMessage = {
        id,
        role: 'agent',
        content: '',
        streaming: true,
        timestamp: Date.now(),
        sortIndex: seq,
      }
      return {
        messages: [...s.messages, msg],
        streamingMessageId: id,
        timelineSeq: seq,
      }
    })
    return id
  },

  appendToken: (token) => {
    const { streamingMessageId } = get()
    if (!streamingMessageId) return
    set((s) => ({
      messages: s.messages.map((m) =>
        m.id === streamingMessageId ? { ...m, content: m.content + token } : m,
      ),
    }))
  },

  endStreaming: (markAsThinking = false) => {
    const { streamingMessageId } = get()
    if (!streamingMessageId) return
    set((s) => {
      const id = streamingMessageId
      const messages = s.messages
        .map((m) =>
          m.id === id
            ? { ...m, streaming: false, streamedAsThinking: markAsThinking || m.streamedAsThinking }
            : m,
        )
        .filter(
          (m) =>
            !(
              m.id === id &&
              m.role === 'agent' &&
              m.content.trim() === ''
            ),
        )
      return { messages, streamingMessageId: null }
    })
  },

  attachReceiptToLastTurn: ({ runId, attestationPath }) =>
    set((s) => {
      const messages = [...s.messages]
      for (let i = messages.length - 1; i >= 0; i--) {
        if (messages[i].role === 'agent') {
          messages[i] = { ...messages[i], runId, attestationPath }
          return { messages }
        }
      }
      const seq = s.timelineSeq + 1
      messages.push({
        id: crypto.randomUUID(),
        role: 'agent',
        content: '',
        timestamp: Date.now(),
        sortIndex: seq,
        runId,
        attestationPath,
      })
      return { messages, timelineSeq: seq }
    }),

  setAgents: (agents) => set({ agents }),

  updateAgent: (agentId, patch) =>
    set((s) => ({
      agents: s.agents.map((a) =>
        a.agent_id === agentId ? { ...a, ...patch } : a,
      ),
    })),

  setChecklist: (tasks) => set({ checklist: tasks ?? [] }),

  setArtifacts: (artifacts) => set({ artifacts: artifacts ?? [] }),

  updateChecklist: (tasks) =>
    set((s) => {
      const list = tasks ?? []
      const map = new Map((s.checklist ?? []).map((t) => [t.id, t]))
      list.forEach((t) => map.set(t.id, t))
      return { checklist: Array.from(map.values()) }
    }),

  addArtifact: (artifact) =>
    set((s) => ({ artifacts: [...s.artifacts, artifact] })),

  addInterrupt: (interrupt) =>
    set((s) => ({ interrupts: [...s.interrupts, interrupt] })),

  appendSessionLog: (message) =>
    set((s) => ({
      sessionLogs: [
        ...s.sessionLogs,
        { id: crypto.randomUUID(), timestamp: Date.now(), message },
      ],
    })),

  setTokenUsage: (usage) =>
    set({
      tokenUsage: {
        input: coerceTokenCount(usage.input),
        output: coerceTokenCount(usage.output),
        total: coerceTokenCount(usage.total),
      },
    }),

  mergeTokenUsage: (usage) =>
    set((s) => {
      const prevIn = coerceTokenCount(s.tokenUsage.input)
      const prevOut = coerceTokenCount(s.tokenUsage.output)
      const prevTot = coerceTokenCount(s.tokenUsage.total)
      const incIn = coerceTokenCount(usage.input)
      const incOut = coerceTokenCount(usage.output)
      const incTot = coerceTokenCount(usage.total)
      return {
        tokenUsage: {
          input: Math.max(prevIn, incIn),
          output: Math.max(prevOut, incOut),
          total: Math.max(prevTot, incTot),
        },
      }
    }),
  setElapsed: (seconds) => set({ elapsedSeconds: seconds }),

  openCredentialsModal: () => set({ credentialsModalOpen: true }),
  closeCredentialsModal: () => set({ credentialsModalOpen: false }),
  setByokActive: (sessionId) => set({ byokSessionId: sessionId }),
  clearByokActive: () => set({ byokSessionId: null }),
  openSaveWorkflowModal: () => set({ saveWorkflowModalOpen: true }),
  closeSaveWorkflowModal: () => set({ saveWorkflowModalOpen: false }),
  openCrmModal: () => set({ crmModalOpen: true }),
  closeCrmModal: () => set({ crmModalOpen: false }),
  openIntegrationsModal: () => set({ integrationsModalOpen: true }),
  closeIntegrationsModal: () => set({ integrationsModalOpen: false }),

  toolTraceStart: (p) =>
    set((s) => {
      const activeAgentName = p.agent_id || p.tool_name
      const idx = s.toolTrace.findIndex((t) => t.call_id === p.call_id)
      const seq = s.timelineSeq + 1
      const startedAt = Date.now()
      const row: ToolInvocationTrace = {
        call_id: p.call_id,
        tool_name: p.tool_name,
        agent_id: p.agent_id,
        phase: 'running',
        inputs: p.inputs && typeof p.inputs === 'object' ? p.inputs : {},
        progressLines: [],
        content_preview: '',
        protocol: p.protocol,
        sortIndex: seq,
        startedAt,
      }
      if (idx >= 0) {
        const next = [...s.toolTrace]
        const prev = next[idx]
        next[idx] = {
          ...prev,
          ...row,
          sortIndex: prev.sortIndex ?? row.sortIndex,
          startedAt: prev.startedAt ?? row.startedAt,
        }
        return { toolTrace: next, runPhase: 'executing', activeAgentName }
      }
      return { toolTrace: [...s.toolTrace, row], timelineSeq: seq, runPhase: 'executing', activeAgentName }
    }),

  toolTraceProgress: (callId, line) =>
    set((s) => ({
      toolTrace: s.toolTrace.map((t) =>
        t.call_id === callId
          ? { ...t, progressLines: [...t.progressLines, line] }
          : t,
      ),
    })),

  toolTraceRetry: (p) =>
    set((s) => ({
      toolTrace: s.toolTrace.map((t) =>
        t.call_id === p.call_id
          ? {
              ...t,
              phase: 'running',
              retryAttempt: p.attempt,
              maxAttempts: p.max_attempts,
              progressLines: [
                ...t.progressLines,
                `Retrying (${p.attempt}/${p.max_attempts})${p.reason ? ` — ${p.reason}` : ''}`,
              ],
            }
          : t,
      ),
    })),

  toolTraceFinalize: (p) =>
    set((s) => {
      const phase: ToolInvocationPhase = p.status === 'error' ? 'error' : 'success'
      return {
        toolTrace: s.toolTrace.map((t) =>
          t.call_id === p.call_id
            ? {
                ...t,
                phase,
                content_preview: p.content_preview ?? t.content_preview,
                tool_name: p.tool_name || t.tool_name,
                agent_id: p.agent_id || t.agent_id,
                total_cost_usd: p.total_cost_usd ?? t.total_cost_usd,
                base_fee: p.base_fee ?? t.base_fee,
                verified: p.verified ?? t.verified,
                verdict_reason: p.verdict_reason ?? t.verdict_reason,
                // Clear the retry indicator once the final result lands.
                retryAttempt: undefined,
                maxAttempts: undefined,
              }
            : t,
        ),
      }
    }),

  clearToolTrace: () => set({ toolTrace: [], traceBuffer: [] }),

  recordTraceEvent: (type) =>
    set((s) => ({
      traceBuffer: [
        ...s.traceBuffer.slice(-(TRACE_BUFFER_MAX - 1)),
        { id: crypto.randomUUID(), type, timestamp: Date.now() },
      ],
    })),

  addCanvas: (entry) =>
    set((s) => ({
      canvases: [...s.canvases, entry],
      timelineSeq: entry.sortIndex ?? s.timelineSeq + 1,
    })),

  hydrateFromSessionStatus: (data) => {
    if (data.status === 'not_found') return
    set((s) => {
      let nextStatus = s.status
      const terminal: SessionStatus[] = ['complete', 'failed']
      const details = reduceSessionDetailsFromStatus(
        {
          agents: s.agents,
          checklist: s.checklist,
          artifacts: s.artifacts,
          tokenUsage: s.tokenUsage,
          sessionLogs: s.sessionLogs,
        },
        data,
      )
      const mappedInterrupts: Interrupt[] = details.interrupts ?? []
      if (!terminal.includes(s.status)) {
        if (mappedInterrupts.length > 0) {
          nextStatus = 'interrupted'
        } else if (
          data.status === 'ready' &&
          s.status === 'interrupted' &&
          !s.streamingMessageId
        ) {
          nextStatus = 'idle'
        }
      }
      return {
        agents: details.agents,
        checklist: details.checklist,
        artifacts: details.artifacts,
        tokenUsage: details.tokenUsage,
        sessionLogs: details.sessionLogs,
        interrupts: mappedInterrupts,
        status: nextStatus,
      }
    })
  },

  hydrateFromTranscript: (sessionId, entries) => {
    if (entries.length === 0) return
    set(() => {
      const messages: ChatMessage[] = []
      const toolTrace: ToolInvocationTrace[] = []
      let maxSeq = 0
      for (const e of entries) {
        maxSeq = Math.max(maxSeq, e.sequence_num)
        const ts = Date.parse(e.created_at)
        const timestamp = Number.isNaN(ts) ? Date.now() : ts
        const stableId = `${sessionId}-seq-${e.sequence_num}-${e.role}`
        if (e.role === 'USER') {
          let attachedArtifacts: AttachedArtifactRef[] | undefined
          const rawAtt = e.tool_inputs?.artifact_attachments
          if (Array.isArray(rawAtt)) {
            const parsed = rawAtt
              .filter(
                (x): x is Record<string, unknown> =>
                  x !== null && typeof x === 'object',
              )
              .map((x) => ({
                artifact_id: String(x.artifact_id ?? ''),
                filename: String(x.filename ?? ''),
                mime_type: String(x.mime_type ?? ''),
                size_bytes: Number(x.size_bytes ?? 0),
              }))
              .filter((a) => a.artifact_id.length > 0)
            if (parsed.length > 0) attachedArtifacts = parsed
          }
          messages.push({
            id: stableId,
            role: 'user',
            content: e.content,
            timestamp,
            sortIndex: e.sequence_num,
            attachedArtifacts,
          })
        } else if (e.role === 'ASSISTANT') {
          messages.push({
            id: stableId,
            role: 'agent',
            content: e.content,
            timestamp,
            sortIndex: e.sequence_num,
          })
        } else {
          const phase: ToolInvocationPhase =
            e.tool_status === 'error' ? 'error' : 'success'
          const rawMeta = (e.tool_inputs ?? {}) as Record<string, unknown>
          const agentIdRaw = rawMeta.agent_id
          const agentId =
            typeof agentIdRaw === 'string' && agentIdRaw.length > 0
              ? agentIdRaw
              : '_external'
          const invArgs = rawMeta.invocation_args
          const inputs: Record<string, unknown> =
            invArgs !== null &&
            typeof invArgs === 'object' &&
            !Array.isArray(invArgs)
              ? { ...(invArgs as Record<string, unknown>) }
              : {}
          const displayToolName =
            typeof e.tool_name === 'string' && e.tool_name.length > 0
              ? e.tool_name
              : 'tool'
          const totalCostRaw = rawMeta.total_cost_usd
          const baseFeeRaw = rawMeta.base_fee
          const protocolRaw = rawMeta.protocol
          toolTrace.push({
            call_id: e.tool_call_id || `hist-${e.sequence_num}`,
            tool_name: displayToolName,
            agent_id: agentId,
            phase,
            inputs,
            progressLines: [],
            content_preview: e.content.slice(0, 500),
            protocol: typeof protocolRaw === 'string' ? protocolRaw : undefined,
            sortIndex: e.sequence_num,
            startedAt: timestamp,
            total_cost_usd: typeof totalCostRaw === 'string' ? totalCostRaw : undefined,
            base_fee: typeof baseFeeRaw === 'string' ? baseFeeRaw : undefined,
          })
        }
      }
      return {
        messages,
        toolTrace,
        timelineSeq: maxSeq,
        streamingMessageId: null,
      }
    })
  },

  reset: () => set(initialState),
}))
