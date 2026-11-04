import { useEffect, useRef, useState } from 'react'

import { cn } from '../cn'
import {
  OWL_VIEWBOX_SIZE,
  BODY_PATH,
  BEAK_PATH,
  FOOT_LEFT,
  FOOT_RIGHT,
  PERCH_RECT,
  EYE_LEFT_CENTER,
  EYE_RIGHT_CENTER,
  EYE_DIAMETER,
} from './owlPaths'
import { buildEye, EYE_GRID_HEIGHT, PUPIL_RX_RATIO, PUPIL_RY_RATIO } from './owlEyes'
import { OwlEarLeft } from './OwlEarLeft'
import { OwlEarRight } from './OwlEarRight'
import { FROWN_PATH } from './owlFrown'
import { buildWings, WING_LEFT_ORIGIN, WING_RECT_SIZE, WING_RIGHT_ORIGIN } from './owlWings'

import './owl-mascot.css'

function EyeLayer({
  side,
  center,
  geometry,
}: {
  side: 'left' | 'right'
  center: { x: number; y: number }
  geometry: ReturnType<typeof buildEye>
}) {
  const scale = EYE_DIAMETER / EYE_GRID_HEIGHT
  const { cells, center: gridCenter } = geometry

  return (
    <g
      className={cn('owl-eye', side === 'left' ? 'owl-eye-left' : 'owl-eye-right')}
      style={{ transformOrigin: `${center.x}px ${center.y}px` }}
      shapeRendering="crispEdges"
    >
      {cells.map((c) => (
        <rect
          key={`${side}-${c.x}-${c.y}`}
          x={center.x + (c.x - gridCenter.x) * scale}
          y={center.y + (c.y - gridCenter.y) * scale}
          width={scale}
          height={scale}
          fill={c.fill}
        />
      ))}
      <ellipse
        className="owl-pupil"
        cx={center.x}
        cy={center.y}
        rx={scale * PUPIL_RX_RATIO}
        ry={scale * PUPIL_RY_RATIO}
      />
    </g>
  )
}

function WingLayer({ side, cells }: { side: 'left' | 'right'; cells: ReturnType<typeof buildWings>['left'] }) {
  const origin = side === 'left' ? WING_LEFT_ORIGIN : WING_RIGHT_ORIGIN
  return (
    <g
      className={cn('owl-wing', side === 'left' ? 'owl-wing-left' : 'owl-wing-right')}
      style={{ transformOrigin: `${origin.x}px ${origin.y}px` }}
      shapeRendering="crispEdges"
    >
      {cells.map((c, i) => (
        <rect key={`${side}-${i}`} x={c.x} y={c.y} width={WING_RECT_SIZE} height={WING_RECT_SIZE} fill={c.fill} />
      ))}
    </g>
  )
}

/** `complete` means the session finished — no check gated it (FR-14). */
export type OwlState = 'idle' | 'executing' | 'complete' | 'error'

export interface OwlMascotProps {
  readonly state?: OwlState
  readonly size?: number
  readonly showPerch?: boolean
  readonly className?: string
}

/** Metis — Orcha's owl mascot (locked SVG, viewBox 0 0 100 100). */
export function OwlMascot({ state = 'idle', size = 64, showPerch = true, className }: OwlMascotProps) {
  const eye = buildEye()
  const wings = buildWings()
  const [wingReveal, setWingReveal] = useState(false)
  const [eyeBlink, setEyeBlink] = useState(false)
  const mounted = useRef(false)

  useEffect(() => {
    if (!mounted.current) {
      mounted.current = true
      return
    }
    setWingReveal(true)
    setEyeBlink(true)
    const timer = window.setTimeout(() => {
      setWingReveal(false)
      setEyeBlink(false)
    }, 650)
    return () => window.clearTimeout(timer)
  }, [state])

  return (
    <svg
      viewBox={`0 0 ${OWL_VIEWBOX_SIZE} ${OWL_VIEWBOX_SIZE}`}
      width={size}
      height={size}
      className={cn(
        'owl-mascot',
        `owl-mascot--${state}`,
        wingReveal && 'owl-mascot--wing-reveal',
        eyeBlink && 'owl-mascot--eye-blink',
        className,
      )}
      role="img"
      aria-label="Metis, the Orcha owl"
    >
      <g className="owl-body-group">
        <WingLayer side="left" cells={wings.left} />
        <WingLayer side="right" cells={wings.right} />
        <path className="owl-torso" d={BODY_PATH} />
        <OwlEarLeft />
        <OwlEarRight />
        <rect className="owl-foot owl-foot-left" {...FOOT_LEFT} />
        <rect className="owl-foot owl-foot-right" {...FOOT_RIGHT} />
        <g className="owl-head-group">
          <path className="owl-frown" d={FROWN_PATH} />
          <g className="owl-beak">
            <path d={BEAK_PATH} />
          </g>
          <EyeLayer side="left" center={EYE_LEFT_CENTER} geometry={eye} />
          <EyeLayer side="right" center={EYE_RIGHT_CENTER} geometry={eye} />
        </g>
      </g>
      {showPerch && <rect className="owl-perch" {...PERCH_RECT} />}
    </svg>
  )
}
