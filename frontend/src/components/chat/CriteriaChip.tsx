import { CRITERIA, useCriteriaStore } from '../../store/criteria'
import { cn } from '../ui/cn'

/**
 * Composer criteria chip — declare what the next run must satisfy. Each pill
 * is a toggle; an active pill is sent as `acceptance_criteria` with the
 * message, evaluated by the runtime and recorded as a signed verdict.
 */
export function CriteriaChip() {
  const selected = useCriteriaStore((s) => s.selected)
  const toggle = useCriteriaStore((s) => s.toggle)
  const count = Object.values(selected).filter(Boolean).length

  return (
    <div
      role="group"
      aria-label="Declared acceptance criteria"
      className="flex h-7 items-center gap-1 rounded-full border border-surface-border bg-surface-elevated px-1.5 text-[11px] font-medium text-text-secondary"
    >
      <span
        className="px-1 text-text-muted"
        title="Declared before the run and recorded in its receipt; a failed criterion refuses settlement"
      >
        criteria{count > 0 ? ` ${count}` : ''}
      </span>
      {CRITERIA.map((c) => {
        const on = selected[c.key]
        return (
          <button
            key={c.key}
            type="button"
            aria-pressed={on}
            title={c.title}
            onClick={() => toggle(c.key)}
            className={cn(
              'h-5 rounded-full px-2 transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-primary',
              on
                ? 'bg-brand-primary-dim text-text-body border border-[var(--accent-border)]'
                : 'border border-transparent hover:border-surface-borderLight hover:text-text-body',
            )}
          >
            {c.label}
          </button>
        )
      })}
    </div>
  )
}
