import type { MarkdownCardSpec } from '../../types/canvas'
import { ChatMarkdown } from '../chat/ChatMarkdown'

export function MarkdownCard({ spec }: { spec: MarkdownCardSpec }) {
  return (
    <div className="flex flex-col gap-1.5 rounded-xl bg-surface-overlay border border-surface-border px-5 py-4 shadow-sm">
      {spec.title && (
        <span className="text-[10px] font-semibold uppercase tracking-widest text-text-secondary">
          {spec.title}
        </span>
      )}
      {/*
        Rendered as markdown, not as pre-wrapped text. The body is declared as
        a markdown string, so headings, lists, links and code have to resolve;
        rendering it verbatim showed agents' `#` and `*` characters on screen.
        Reuses the renderer the chat transcript already uses so a heading looks
        the same wherever it appears.
      */}
      <div className="text-[13px] text-text-secondary">
        <ChatMarkdown tone="agent">{spec.body}</ChatMarkdown>
      </div>
    </div>
  )
}
