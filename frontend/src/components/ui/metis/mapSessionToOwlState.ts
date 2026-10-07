import type { SessionStatus } from '../../../types'

import type { OwlState } from './OwlMascot'

/** Map Orcha session status to Metis owl animation state. */
export function mapSessionToOwlState(status: SessionStatus): OwlState {
  switch (status) {
    case 'running':
    case 'interrupted':
      return 'executing'
    case 'complete':
      return 'complete'
    case 'failed':
      return 'error'
    default:
      return 'idle'
  }
}
