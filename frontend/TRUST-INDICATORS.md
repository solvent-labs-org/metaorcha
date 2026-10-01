# Trust indicators — what each one is backed by

FR-14: every trust claim in the UI maps to an enforced check, or is relabelled to
what it actually means. This table is the committed map (story 1.1). **Adding an
indicator means adding a row.** "Verified" appears only where a check gated an
outcome; nothing here says "verified" because something merely succeeded.

Two kinds of check exist today:

- **Structural check** — `services/superagent/.../middleware/pipeline.py`
  `_structural_verify`: the tool's output is well-formed (canvas envelope shape,
  non-empty content). It gates nothing; it is a label on the step.
- **Settle gate** — `pricing/settle_gate.py`, the only check that gates an outcome
  (mock credit is written or refused). Its vocabulary, and the only names an
  indicator may show: SDK verifier `schema`, `steps_root`, `steps_merkle_root`,
  `signature`; gate-side `charter_hash`, `run_id_mismatch`, `missing_attestation`,
  `verify_error`, `signer_did`, `agent_did`, `audit_write_error`, `already_settled`,
  `credit_write_error`, `verdict_fail`. Reaches the UI as `gate` on
  `GET /sessions/{id}/audit` (latest `attested_settlements` row for the session),
  present only when a gate evaluated the run.

| Indicator | Where | Backed by | State (2026-09-25) |
|---|---|---|---|
| "Structurally checked" / "Structurally failed" badge | `components/chat/ToolRunCard.tsx` `VerifiedBadge` | structural check (`verified` + `verdict_reason` on the step) | relabelled in #75 (`d1e992d`); unchanged |
| Per-step dot + label | `components/workbench/RunTab.tsx` `RunStepRow` | structural check via the audit `steps[]`; `tool_status` is only success/error | **relabelled here** — was green + "verified" when the tool merely succeeded; now: failed → red "failed"; structural pass → blue "structurally checked"; success alone → neutral dot, no label |
| Run-level "settled" / "refused — `<check>`" | `components/workbench/RunTab.tsx` `GateIndicator` | settle gate (`audit.gate`) | **added here** — the one indicator backed by a check that gates an outcome; names the first failed check; absent when no gate ran |
| Owl mascot state | `components/ui/metis/OwlMascot.tsx`, `mapSessionToOwlState.ts` | nothing — it mirrors session status | **renamed** `verified` → `complete` (status `complete` → owl `complete`); green stays, the word does not |
| Owl preview copy "green verified" | `pages/OwlPreview.tsx` | nothing | **relabelled** "green complete" |
| Home tagline "verified multi-protocol run out" | `pages/Home.tsx` | nothing (attestation is flag-gated, the gate is flag-gated) | **relabelled** "recorded multi-protocol run out" |
| Receipt download comment "the file is not marked verified" | `lib/downloadReceipt.ts` | — | honest as written; unchanged |

Recorded, not changed here (outside the frontend; owned by story 3.2, export copy):

- Audit package fields `summary.steps_verified` / `steps_failed` and its `note`
  ("Verified Runs") describe the structural check, not the gate.
- Sandbox mailer receipt (`system_tools/mailer.py`) prints "verified" / "failed"
  per step from the same structural field.
