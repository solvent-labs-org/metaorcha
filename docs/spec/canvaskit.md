# CanvasKit

**The declarative UI protocol for AI-native applications.**

> "shadcn/ui for AI-native apps — agents emit structured manifests, a curated component library renders them."

The problem with AI-generated UIs: LLMs generate brittle React code that breaks on edge cases, is impossible to maintain, and produces inconsistent visual results. Users don't want generated code — they want a genuinely excellent interface.

CanvasKit solves this correctly. Agents emit **structured data** (a `UIManifest`). A curated runtime component library renders it. Clean separation. The agent decides *what* to show; CanvasKit decides *how* to show it beautifully.

---

## How it works

```
Agent executes task
  ↓
Agent emits: canvas_manifest SSE event { manifest_id, manifest: UIManifest }
  ↓
Frontend receives: CanvasRenderer maps UIManifest → React components
  ↓
User sees: real application UI — not a chat message, not code
```

An agent that returns a portfolio summary no longer returns markdown text. It returns a `UIManifest` that renders as a MetricCard grid + LineChart + DataTable of positions.

---

## UIManifest Schema

```typescript
interface UIManifest {
  version: "1.0"
  title?: string
  layout: "dashboard" | "single" | "table" | "timeline"
  components: CanvasComponent[]
}
```

**`layout` values:**
- `"dashboard"` — 2-column grid; wide components span full width
- `"single"` — full-width stack of components
- `"table"` — full-width; optimized for DataTable as primary content
- `"timeline"` — full-width; optimized for Timeline as primary content

---

## Component Library

### `metric_card`
A single KPI with label, value, trend arrow, and optional delta.

```json
{
  "type": "metric_card",
  "id": "portfolio_value",
  "label": "Portfolio Value",
  "value": 47230,
  "unit": "USD",
  "delta": 2.3,
  "trend": "up",
  "sub_label": "↑ 2.3% today"
}
```

### `stat_grid`
A responsive grid of metrics — 2, 3, or 4 columns.

```json
{
  "type": "stat_grid",
  "id": "market_overview",
  "columns": 3,
  "stats": [
    { "label": "S&P 500", "value": 5234, "delta": 0.8 },
    { "label": "BTC", "value": 67420, "unit": "USD", "delta": -1.2 },
    { "label": "ETH", "value": 3890, "unit": "USD", "delta": 4.1 }
  ]
}
```

### `line_chart`
Time-series chart. Multiple series supported.

```json
{
  "type": "line_chart",
  "id": "portfolio_chart",
  "title": "90-Day Performance",
  "x_key": "date",
  "y_keys": ["value"],
  "data": [
    { "date": "2026-03-01", "value": 41200 },
    { "date": "2026-04-01", "value": 44100 },
    { "date": "2026-05-01", "value": 47230 }
  ]
}
```

### `data_table`
Sortable table with typed columns. Column types: `text`, `number`, `currency`, `date`, `percent`.

```json
{
  "type": "data_table",
  "id": "positions",
  "title": "Positions",
  "columns": [
    { "key": "symbol", "label": "Symbol", "type": "text" },
    { "key": "value",  "label": "Value",  "type": "currency" },
    { "key": "change", "label": "Change", "type": "percent" }
  ],
  "rows": [
    { "symbol": "AAPL", "value": 12400, "change": 1.2 },
    { "symbol": "ETH",  "value": 8900,  "change": -3.1 }
  ]
}
```

### `alert_feed`
Notification/event feed with severity levels: `info`, `warning`, `error`, `success`.

```json
{
  "type": "alert_feed",
  "id": "alerts",
  "title": "Alerts",
  "alerts": [
    {
      "id": "eth_drop",
      "severity": "warning",
      "title": "ETH dropped 8.2% since yesterday",
      "body": "Current: $3,890. Your alert threshold: -5%.",
      "timestamp": "09:14"
    }
  ]
}
```

### `pie_chart`
Donut/pie chart for allocations.

```json
{
  "type": "pie_chart",
  "id": "allocation",
  "title": "Portfolio Allocation",
  "data": [
    { "label": "Stocks", "value": 28700 },
    { "label": "Crypto", "value": 14200 },
    { "label": "Cash",   "value": 4330 }
  ]
}
```

### `progress_bar`
Labeled progress indicator with color states: `default`, `success`, `warning`, `error`.

```json
{
  "type": "progress_bar",
  "id": "budget_progress",
  "label": "Monthly Budget",
  "value": 1840,
  "max": 2500,
  "color": "warning"
}
```

### `timeline`
Chronological event list with step status: `complete`, `active`, `pending`.

```json
{
  "type": "timeline",
  "id": "sync_log",
  "title": "Last Sync",
  "events": [
    { "id": "1", "label": "Fetched Alpaca positions", "status": "complete", "timestamp": "09:00:01" },
    { "id": "2", "label": "Fetched Coinbase balances", "status": "complete", "timestamp": "09:00:03" },
    { "id": "3", "label": "Updating portfolio value",  "status": "active" },
    { "id": "4", "label": "Send alert notifications",  "status": "pending" }
  ]
}
```

### `markdown_card`
Rich formatted text — headings, lists, links, inline and fenced code, blockquotes
and GFM tables. Use it for research summaries and narrative output, rather than
faking structure inside another component.

```json
{
  "type": "markdown_card",
  "id": "research_summary",
  "title": "Q3 Findings",
  "body": "## Summary\n\nRevenue grew **12%** QoQ.\n\n- Retention up 4pts\n- Churn flat\n\nSee the [full report](https://example.com/q3)."
}
```

The markdown string is carried in `body`. It renders through the same markdown
renderer as the chat transcript, so a heading looks the same wherever it appears.
Note the flat-field rule: the markdown goes directly in `body`, not nested under
`props`.

---

## SSE Event

Agents send a `canvas_manifest` SSE event. The frontend handles it automatically.

```json
{
  "type": "canvas_manifest",
  "manifest_id": "finance-dashboard-1",
  "manifest": {
    "version": "1.0",
    "title": "Alex's Finance Tracker",
    "layout": "dashboard",
    "components": [...]
  }
}
```

The manifest is interleaved into the chat timeline at its SSE arrival position — alongside messages and tool runs.

---

## Building a new component

1. Add the spec type to `frontend/src/types/canvas.ts` (extend the `CanvasComponent` union)
2. Build the React component in `frontend/src/components/canvas/<ComponentName>.tsx`
3. Register it in `CanvasRenderer.tsx` (`switch` on `spec.type`)
4. Export from `frontend/src/components/canvas/index.ts`
5. Add it to this doc with a JSON example

**Quality bar:** A new component must look better than what a developer would build in a weekend. That is the floor. CanvasKit components are what users see every day — they must be excellent.

---

## Developer earning model *(roadmap — not active in v0.1)*

> **Status: roadmap.** The billing infrastructure does not exist yet. Components today earn reputation and adoption; the fee layer activates when a paid app runtime ships.

Every component in the CanvasKit ecosystem is designed to earn when it renders in a deployed app:

| Contribution | Earning | Split |
|--------------|---------|-------|
| Core component (MetricCard, LineChart, etc.) | Per-render fee | 80% dev / 20% platform |
| Domain-specific component (e.g., CandlestickChart) | Per-render fee | 80% dev / 20% platform |
| Theme/variant | Per-render fee | 80% dev / 20% platform |

A `MetricCard` that renders 10,000 times/day in deployed apps would earn its author automatically — no marketplace listing required, no sales, just usage. This is the design target; the mechanism ships with the app-builder runtime.

---

## Open questions → RFC issues

- [ ] **Animation:** Should components support entrance animations? (e.g., count-up for MetricCard values) Risk: perceived jank vs. genuine delight.
- [ ] **Interactivity:** Should DataTable support click-to-drill-down that fires a new agent invocation? Architecture implication: components would need to emit events back up.
- [ ] **Real-time binding:** Dashboard apps need live data (e.g., portfolio value updating every 30s). Should `UIManifest` support a `refresh_interval` + data binding back to an agent endpoint? Or is re-emission of a new `canvas_manifest` the right model?
- [ ] **Theming:** Should a `UIManifest` carry a `theme` field (finance, health, productivity) that controls color palette? Or does the global Orcha theme always apply?
