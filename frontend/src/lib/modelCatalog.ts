export type CostTier = 'low' | 'mid' | 'high'

export interface CatalogModel {
  id: string
  label: string
  tier: CostTier
}

/** OpenRouter-shaped ids with a coarse cost band. Not a price quote. */
export const CATALOG_MODELS: CatalogModel[] = [
  {
    id: 'meta-llama/llama-3.1-8b-instruct',
    label: 'Llama 3.1 8B',
    tier: 'low',
  },
  {
    id: 'anthropic/claude-haiku-4.5',
    label: 'Claude Haiku',
    tier: 'low',
  },
  {
    id: 'google/gemini-2.5-flash',
    label: 'Gemini Flash',
    tier: 'low',
  },
  {
    id: 'openai/gpt-4o-mini',
    label: 'GPT-4o mini',
    tier: 'mid',
  },
  {
    id: 'openai/gpt-4o',
    label: 'GPT-4o',
    tier: 'high',
  },
  {
    id: 'anthropic/claude-sonnet-4.6',
    label: 'Claude Sonnet',
    tier: 'high',
  },
]

export const TIER_LABEL: Record<CostTier, string> = {
  low: 'low cost',
  mid: 'mid cost',
  high: 'higher cost',
}

export const OTHER_MODEL_ID = 'other'

export function catalogEntry(modelId: string): CatalogModel | undefined {
  return CATALOG_MODELS.find((m) => m.id === modelId)
}
