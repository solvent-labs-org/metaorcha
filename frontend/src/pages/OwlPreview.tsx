import { useState } from 'react'

import { OwlMascot, type OwlState } from '../components/ui/OwlMascot'
import { Button } from '../components/ui/Button'

const STATES: OwlState[] = ['idle', 'executing', 'complete', 'error']

/**
 * Dev-only playground for Metis (the owl mascot) — mounted at /dev/owl in
 * development builds only (see App.tsx).
 */
export function OwlPreview() {
  const [state, setState] = useState<OwlState>('idle')
  const [showPerch, setShowPerch] = useState(true)
  const [size, setSize] = useState(160)

  return (
    <div className="flex min-h-full flex-col items-center gap-8 bg-surface-canvas p-10 text-text-body">
      <h1 className="text-heading text-xl font-semibold">Metis — owl mascot playground</h1>

      <div className="flex items-center justify-center rounded-lg border border-surface-border bg-surface-base p-10">
        <OwlMascot state={state} size={size} showPerch={showPerch} />
      </div>

      <div className="flex flex-wrap items-center justify-center gap-2">
        {STATES.map((s) => (
          <Button
            key={s}
            variant={s === state ? 'primary' : 'secondary'}
            onClick={() => setState(s)}
          >
            {s}
          </Button>
        ))}
      </div>

      <div className="flex items-center gap-4 text-caption text-text-secondary">
        <label className="flex items-center gap-2">
          <input
            type="checkbox"
            checked={showPerch}
            onChange={(e) => setShowPerch(e.target.checked)}
          />
          perch
        </label>
        <label className="flex items-center gap-2">
          size
          <input
            type="range"
            min={48}
            max={320}
            value={size}
            onChange={(e) => setSize(Number(e.target.value))}
          />
          {size}px
        </label>
      </div>

      <p className="max-w-lg text-center text-caption text-text-secondary">
        Locked Metis design — black idle · yellow executing · green complete · red error.
        Blinks and wing reveal on state change. Source: <code>components/ui/metis/</code>.
      </p>
    </div>
  )
}
