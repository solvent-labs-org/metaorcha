import { useState } from 'react'
import { downloadReceipt } from '../../lib/downloadReceipt'

interface DownloadReceiptButtonProps {
  readonly runId: string
}

export function DownloadReceiptButton({
  runId,
}: Readonly<DownloadReceiptButtonProps>) {
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const handleClick = async () => {
    setError(null)
    setBusy(true)
    try {
      await downloadReceipt(runId)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Download failed')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="flex flex-col items-start gap-1">
      <button
        type="button"
        onClick={() => void handleClick()}
        disabled={busy}
        aria-label="Download receipt"
        title="Download the sealed run envelope"
        className="flex items-center gap-1.5 h-8 px-3 rounded-md bg-surface-overlay border border-surface-borderLight text-[12px] font-medium text-text-body hover:border-surface-muted disabled:opacity-50 transition-colors"
      >
        Download receipt
      </button>
      {error ? (
        <span className="font-mono text-[11px] text-semantic-error">{error}</span>
      ) : null}
    </div>
  )
}
