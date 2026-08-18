// ── CanvasKit v0.1 — UIManifest type system ──────────────────────────────────
// Agents emit a UIManifest via the `canvas_manifest` SSE event.
// CanvasRenderer maps it to real React components.

export type CanvasLayout = 'dashboard' | 'single' | 'table' | 'timeline'

export interface MetricCardSpec {
  type: 'metric_card'
  id: string
  label: string
  value: string | number
  unit?: string
  delta?: number
  trend?: 'up' | 'down' | 'flat'
  sub_label?: string
}

export interface LineChartSpec {
  type: 'line_chart'
  id: string
  title?: string
  x_key: string
  y_keys: string[]
  data: Record<string, unknown>[]
  colors?: string[]
}

export interface DataTableSpec {
  type: 'data_table'
  id: string
  title?: string
  columns: { key: string; label: string; type?: 'text' | 'number' | 'currency' | 'date' | 'percent' }[]
  rows: Record<string, unknown>[]
}

export interface AlertFeedSpec {
  type: 'alert_feed'
  id: string
  title?: string
  alerts: {
    id: string
    severity: 'info' | 'warning' | 'error' | 'success'
    title: string
    body?: string
    timestamp?: string
  }[]
}

export interface PieChartSpec {
  type: 'pie_chart'
  id: string
  title?: string
  data: { label: string; value: number; color?: string }[]
}

export interface StatGridSpec {
  type: 'stat_grid'
  id: string
  columns?: 2 | 3 | 4
  stats: { label: string; value: string | number; delta?: number; unit?: string }[]
}

export interface ProgressBarSpec {
  type: 'progress_bar'
  id: string
  label: string
  value: number
  max?: number
  color?: 'default' | 'success' | 'warning' | 'error'
}

export interface TimelineSpec {
  type: 'timeline'
  id: string
  title?: string
  events: {
    id: string
    label: string
    timestamp?: string
    description?: string
    status?: 'complete' | 'active' | 'pending'
  }[]
}

export interface MarkdownCardSpec {
  type: 'markdown_card'
  id: string
  title?: string
  body: string
}

export type CanvasComponent =
  | MetricCardSpec
  | LineChartSpec
  | DataTableSpec
  | AlertFeedSpec
  | PieChartSpec
  | StatGridSpec
  | ProgressBarSpec
  | TimelineSpec
  | MarkdownCardSpec

export interface UIManifest {
  version: '1.0'
  /** Stable manifest identity; when set the runtime uses it as manifest_id. */
  id?: string
  title?: string
  layout: CanvasLayout
  components: CanvasComponent[]
}

export interface CanvasEntry {
  id: string
  manifest: UIManifest
  sortIndex: number
}
