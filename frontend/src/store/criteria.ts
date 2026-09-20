import { create } from 'zustand'

/**
 * Declared acceptance criteria for the next turns.
 *
 * A criterion is declared before the run and evaluated by the runtime; the
 * result is recorded as a signed verdict in the run's receipt, and a `fail`
 * refuses settlement. Keys and semantics are the runtime's
 * (`SUPPORTED_CRITERIA`): `citations_required`, `exit_zero`.
 *
 * Persisted per browser so a declared criterion survives a reload; it applies
 * to every turn sent from this composer while toggled on.
 */
export type CriterionKey = 'citations_required' | 'exit_zero'

export const CRITERIA: ReadonlyArray<{
  key: CriterionKey
  label: string
  title: string
}> = [
  {
    key: 'citations_required',
    label: 'citations',
    title: 'Each step must carry citations — recorded as a verdict; a miss refuses settlement',
  },
  {
    key: 'exit_zero',
    label: 'tests exit 0',
    title: 'A step must report an exit code of 0 — recorded as a verdict; a miss refuses settlement',
  },
]

interface CriteriaState {
  selected: Record<CriterionKey, boolean>
  toggle: (key: CriterionKey) => void
  /** Request payload, or undefined when nothing is declared. */
  toPayload: () => Record<string, boolean> | undefined
}

const STORAGE_KEY = 'orcha_criteria'
const NONE: Record<CriterionKey, boolean> = {
  citations_required: false,
  exit_zero: false,
}

function loadStored(): Record<CriterionKey, boolean> {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (!raw) return NONE
    const parsed = JSON.parse(raw) as Partial<Record<CriterionKey, unknown>>
    return {
      citations_required: parsed.citations_required === true,
      exit_zero: parsed.exit_zero === true,
    }
  } catch {
    return NONE
  }
}

export const useCriteriaStore = create<CriteriaState>((set, get) => ({
  selected: loadStored(),

  toggle: (key) => {
    const selected = { ...get().selected, [key]: !get().selected[key] }
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(selected))
    } catch {
      // storage unavailable — the toggle still applies to this session
    }
    set({ selected })
  },

  toPayload: () => {
    const on = Object.entries(get().selected).filter(([, v]) => v)
    if (on.length === 0) return undefined
    return Object.fromEntries(on.map(([k]) => [k, true]))
  },
}))
