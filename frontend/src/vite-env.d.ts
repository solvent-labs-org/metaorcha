/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** Base URL of the lead-gen agent (tool-settings, CRM OAuth, etc.). No trailing slash. */
  readonly VITE_LEAD_GEN_URL?: string
  /** Hosted sandbox mode: auto guest session, message-capped ("true" | undefined). */
  readonly VITE_SANDBOX_MODE?: string
  /** Self-hosted local mode: persistent single-user session, no signup ("true" | undefined). */
  readonly VITE_LOCAL_MODE?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
