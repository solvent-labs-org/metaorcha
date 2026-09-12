import { useState } from 'react'
import { useByokStore } from '../../store/byok'
import type { ByokMode } from '../../store/byok'
import { useSessionStore } from '../../store/session'
import {
  CATALOG_MODELS,
  OTHER_MODEL_ID,
  TIER_LABEL,
  catalogEntry,
} from '../../lib/modelCatalog'
import { Button } from '../ui/Button'
import { cn } from '../ui/cn'

const ONBOARDED_KEY = 'orcha_onboarded'

/**
 * First-visit onboarding: pick the hosted free tier or bring your own key,
 * plus an optional system prompt. Re-openable from the composer model chip.
 * The API key is stored locally and only ever sent to the credentials API.
 */
export function OnboardingModal() {
  const isOpen = useByokStore((s) => s.onboardingOpen)
  if (!isOpen) return null
  // Mounted fresh on each open, so form state initializes from the saved config.
  return <OnboardingForm />
}

function OnboardingForm() {
  const close = useByokStore((s) => s.closeOnboarding)
  const saveConfig = useByokStore((s) => s.saveConfig)

  const saved = useByokStore.getState()
  const [mode, setMode] = useState<ByokMode>(saved.mode)
  const [baseUrl, setBaseUrl] = useState(saved.baseUrl)
  const [apiKey, setApiKey] = useState(saved.apiKey)
  const [model, setModel] = useState(saved.model)
  const knownModel = Boolean(catalogEntry(saved.model))
  const [modelChoice, setModelChoice] = useState(
    knownModel ? saved.model : OTHER_MODEL_ID,
  )
  const [systemPrompt, setSystemPrompt] = useState(saved.systemPrompt)

  const byokValid =
    baseUrl.trim().length > 0 && apiKey.trim().length > 0 && model.trim().length > 0
  const canStart = mode === 'hosted' || byokValid

  const handleStart = () => {
    if (!canStart) return
    saveConfig({
      mode,
      baseUrl: baseUrl.trim(),
      apiKey: apiKey.trim(),
      model: model.trim(),
      systemPrompt: systemPrompt.trim(),
    })
    localStorage.setItem(ONBOARDED_KEY, 'true')
    // Apply immediately when a session is already active (no-op when hosted).
    const sessionId = useSessionStore.getState().sessionId
    if (sessionId) void useByokStore.getState().applyToSession(sessionId)
    close()
  }

  const handleSkip = () => {
    localStorage.setItem(ONBOARDED_KEY, 'true')
    close()
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center"
      role="dialog"
      aria-modal="true"
      aria-label="Set up your model"
      onKeyDown={(e) => {
        if (e.key === 'Escape') handleSkip()
      }}
    >
      {/* Backdrop */}
      <div
        className="absolute inset-0 bg-surface-canvas/70"
        onClick={handleSkip}
        aria-hidden="true"
      />

      {/* Modal */}
      <div className="relative w-[440px] max-h-[90vh] flex flex-col bg-surface-elevated border border-surface-border rounded-xl shadow-lg overflow-hidden">
        {/* Header */}
        <div className="px-5 pt-5 pb-1 shrink-0">
          <p className="text-[15px] font-semibold text-text-heading">Set up your model</p>
          <p className="mt-1 text-caption text-text-secondary">
            Start on the hosted free tier, or point Orcha at your own provider.
          </p>
        </div>

        <div className="overflow-y-auto scrollbar-thin flex-1 px-5 py-4 flex flex-col gap-4">
          {/* Model choice */}
          <div role="radiogroup" aria-label="Model provider" className="flex flex-col gap-2">
            {(
              [
                { value: 'hosted', label: 'Hosted free tier', hint: 'no key needed' },
                { value: 'byok', label: 'Bring your own key', hint: 'any OpenAI-compatible endpoint' },
              ] as const
            ).map((opt) => (
              <label
                key={opt.value}
                className={cn(
                  'flex items-center gap-2.5 rounded-md border px-3 py-2.5 cursor-pointer transition-colors',
                  mode === opt.value
                    ? 'border-[var(--accent-border)] bg-brand-primary-dim'
                    : 'border-surface-border bg-surface-overlay hover:border-surface-borderLight',
                )}
              >
                <input
                  type="radio"
                  name="model-mode"
                  value={opt.value}
                  checked={mode === opt.value}
                  onChange={() => setMode(opt.value)}
                  className="accent-[var(--brand-primary)]"
                />
                <span className="flex-1 text-label text-text-heading">{opt.label}</span>
                <span className="text-[11px] text-text-disabled">{opt.hint}</span>
              </label>
            ))}
          </div>

          {/* BYOK fields */}
          {mode === 'byok' && (
            <div className="flex flex-col gap-2">
              <input
                type="text"
                value={baseUrl}
                onChange={(e) => setBaseUrl(e.target.value)}
                placeholder="Base URL"
                aria-label="Model base URL"
                className="w-full h-10 px-3 rounded-md bg-surface-base border border-surface-borderLight text-label text-text-body placeholder:text-text-disabled focus:outline-none focus:border-brand-primary"
              />
              <input
                type="password"
                value={apiKey}
                onChange={(e) => setApiKey(e.target.value)}
                placeholder="API key…"
                aria-label="Model API key"
                autoComplete="off"
                className="w-full h-10 px-3 rounded-md bg-surface-base border border-surface-borderLight text-label text-text-body placeholder:text-text-disabled focus:outline-none focus:border-brand-primary"
              />
              <div
                role="radiogroup"
                aria-label="Model"
                className="flex flex-col gap-1.5"
              >
                {CATALOG_MODELS.map((opt) => (
                  <label
                    key={opt.id}
                    className={cn(
                      'flex items-center gap-2 rounded-md border px-3 py-2 cursor-pointer transition-colors',
                      modelChoice === opt.id
                        ? 'border-[var(--accent-border)] bg-brand-primary-dim'
                        : 'border-surface-border bg-surface-overlay hover:border-surface-borderLight',
                    )}
                  >
                    <input
                      type="radio"
                      name="model-id"
                      value={opt.id}
                      checked={modelChoice === opt.id}
                      onChange={() => {
                        setModelChoice(opt.id)
                        setModel(opt.id)
                      }}
                      className="accent-[var(--brand-primary)]"
                    />
                    <span className="flex-1 text-label text-text-heading">
                      {opt.label}
                    </span>
                    <span className="text-[11px] text-text-disabled">
                      {TIER_LABEL[opt.tier]}
                    </span>
                  </label>
                ))}
                <label
                  className={cn(
                    'flex items-center gap-2 rounded-md border px-3 py-2 cursor-pointer transition-colors',
                    modelChoice === OTHER_MODEL_ID
                      ? 'border-[var(--accent-border)] bg-brand-primary-dim'
                      : 'border-surface-border bg-surface-overlay hover:border-surface-borderLight',
                  )}
                >
                  <input
                    type="radio"
                    name="model-id"
                    value={OTHER_MODEL_ID}
                    checked={modelChoice === OTHER_MODEL_ID}
                    onChange={() => setModelChoice(OTHER_MODEL_ID)}
                    className="accent-[var(--brand-primary)]"
                  />
                  <span className="flex-1 text-label text-text-heading">Other</span>
                </label>
              </div>
              {modelChoice === OTHER_MODEL_ID && (
                <input
                  type="text"
                  value={model}
                  onChange={(e) => setModel(e.target.value)}
                  placeholder="Model id"
                  aria-label="Model id"
                  className="w-full h-10 px-3 rounded-md bg-surface-base border border-surface-borderLight text-label text-text-body placeholder:text-text-disabled focus:outline-none focus:border-brand-primary"
                />
              )}
              <p className="text-[11px] text-text-disabled">
                Session-scoped — your key is only sent to the credentials vault, never logged.
              </p>
            </div>
          )}

          {/* System prompt */}
          <div className="flex flex-col gap-1.5">
            <label htmlFor="onboarding-system-prompt" className="text-[12px] font-medium text-text-secondary">
              System prompt <span className="text-text-disabled">(optional)</span>
            </label>
            <textarea
              id="onboarding-system-prompt"
              value={systemPrompt}
              onChange={(e) => setSystemPrompt(e.target.value)}
              placeholder="Tell the harness how to behave for your work…"
              rows={3}
              maxLength={2000}
              className="w-full px-3 py-2 rounded-md bg-surface-base border border-surface-border text-label text-text-body placeholder:text-text-disabled focus:outline-none focus:border-brand-primary resize-y"
            />
          </div>
        </div>

        {/* Footer */}
        <div className="flex items-center gap-3 px-5 py-4 border-t border-surface-border shrink-0">
          <Button className="flex-1 h-10" onClick={handleStart} disabled={!canStart}>
            Start
          </Button>
          <button
            onClick={handleSkip}
            className="text-[12px] text-text-disabled hover:text-text-secondary transition-colors"
          >
            Skip
          </button>
        </div>
      </div>
    </div>
  )
}
