# Trust indicators — what each one is backed by

FR-14: every trust claim in the UI maps to an enforced check, or is relabelled to
what it actually means. This table is the committed map (story 1.1). **Adding an
indicator means adding a row.** "Verified" appears only where a check gated an
outcome; nothing here says "verified" because something merely succeeded.

Two kinds of check exist today:

- **Structural check** — `services/superagent/.../middleware/pipeline.py`
  `_structural_verify`: the tool's output is well-formed (canvas envelope shape,
  non-empty content). It gates nothing; it is a label on the step. A platform
  system tool's step (story 3.1, `middleware/system_steps.py`) is in the
  receipt with **no** structural verdict, so it is unchecked, never
  "structurally checked".
- **Settle gate** — `pricing/settle_gate.py`, the only check that gates an outcome
  (mock credit is written or refused). Its vocabulary: SDK verifier `schema`,
  `steps_root`, `steps_merkle_root`, `signature`; gate-side `charter_hash`,
  `run_id_mismatch`, `missing_attestation`, `verify_error`, `signer_did`,
  `agent_did`, `audit_write_error`, `already_settled`, `credit_write_error`,
  `verdict_fail`. The only names an indicator may show are those gate ids, **or
  the names of signed `verdicts[]` entries that failed** (`structural_verification`,
  `declared_acceptance`, `counts_match`), which a routine firing's detail
  substitutes for `verdict_fail` (story 2.5; the ledger keeps `verdict_fail`).
  `GET /sessions/{id}/audit` keys its gate block by the session's latest sealed
  run (`run_id`: the firing's run, else the newest sealed envelope; case
  attestations are not seals), never by the session, so a later unjudged run
  never inherits an earlier run's outcome. `gate` is that run's deciding
  `attested_settlements` row, present only when a gate evaluated it; the UI
  shows no words from it. `settlement` (story 2.6), **firing sessions only**,
  carries the server-built `label` and `statement` from the firing row's state
  and that ledger row: "attested but unsettled" with no gate field when no
  decision is recorded, else "settled" / "refused — `<check>`", with
  " — verdict only, nothing charged" when the deciding row has `call_id` NULL.
  A chat session gets no `settlement` (a paid call there can settle without the
  gate, so nothing is said about it). The routines pane does not read the
  export: it reads the firing row and its run's ledger row
  (`attested_settlements` by `run_id`), through the fields the Gateway computes
  on `last_firing`. Both word their labels with `common/utils/src/firing_view.py`.

| Indicator | Where | Backed by | State (2026-09-25) |
|---|---|---|---|
| "Structurally checked" / "Structurally failed" badge | `components/chat/ToolRunCard.tsx` `VerifiedBadge` | structural check (`verified` + `verdict_reason` on the step) | relabelled in #75 (`d1e992d`); unchanged |
| Per-step dot + label | `components/workbench/RunTab.tsx` `RunStepRow` | structural check via the audit `steps[]`; `tool_status` is only success/error | **relabelled here** — was green + "verified" when the tool merely succeeded; now: failed → red "failed"; structural pass → blue "structurally checked"; success alone → neutral dot, no label |
| Unchecked step (neutral dot, no label) | `components/workbench/RunTab.tsx` `RunStepRow` | a step with no structural verdict (blocked, unresolved or system call, or a row persisted before verdicts existed): the export omits `verified` and counts it in `summary.steps_unchecked` | **corrected in story 2.6** — the export used to default such a step to `verified: true` / "ok"; it now leaves `verified` absent and never counts it in `steps_verified`. The row renders it as success alone: neutral dot, no label |
| Run-level settlement label, e.g. "attested but unsettled" / "settled — verdict only, nothing charged" / "refused — `<check>`" | `components/workbench/RunTab.tsx` `GateIndicator` | `audit.settlement` — the firing row's state and its run's deciding ledger row, worded by the server (`firing_view`) | **story 2.6** — renders `settlement.label` with `settlement.statement` as its title, and derives nothing: the tool tip no longer claims "every check passed", and a non-settled gate row is no longer shown as a refusal. Absent for a chat session and whenever no settlement block was built (nothing sealed, an unreconciled row, or a state other than attested/settled/refused). RunTab is unmounted since `fd5cd3b`; this keeps it honest if remounted |
| Telemetry strip settlement " · `<settlement label>`" | `pages/Chat.tsx` `RunTelemetryStrip` | `audit.settlement.label` (same server label as above) | **added in story 2.6** — appended to the steps/protocols/cost line only when `settlement` is present (firing sessions). A chat page, including one with a paid call, is unchanged |
| Owl mascot state | `components/ui/metis/OwlMascot.tsx`, `mapSessionToOwlState.ts` | nothing — it mirrors session status | **renamed** `verified` → `complete` (status `complete` → owl `complete`); green stays, the word does not |
| Owl preview copy "green verified" | `pages/OwlPreview.tsx` | nothing | **relabelled** "green complete" |
| Home tagline "verified multi-protocol run out" | `pages/Home.tsx` | nothing (attestation is flag-gated, the gate is flag-gated) | **relabelled** "recorded multi-protocol run out" |
| Receipt download comment "the file is not marked verified" | `lib/downloadReceipt.ts` | — | honest as written; unchanged |
| Routine firing state label "last: `<label>`" | `components/layout/RightPanel.tsx` `FiringLines` | the `routine_firings` row's `state` and `detail`, worded by the Gateway (`common/utils/src/firing_view.py` `state_label`) | **added in story 2.5** — replaces the pane's own state → words table. AD-22 vocabulary only (`scheduled`, `running`, `attested but unsettled`, `settled`, `refused`, `paused`, `skipped`, `error`); a refusal reads "refused — `<check>`" (a gate id or a failed verdict name) or "refused — check not recorded", never bare. Never "done", "success" or "verified". The note line under it is the row's detail (error/skipped/paused) or, for an unsettled run whose ledger read found no row, "no settlement decision is recorded for this run" |
| Verdict-only qualifier " · verdict only, nothing charged" | `components/layout/RightPanel.tsx` `FiringLines` | the run's deciding ledger row (`attested_settlements`, any settled row else the oldest) with `call_id` NULL — AD-12 | **added in story 2.5** — shown only on a settled or refused firing; a charged row shows no qualifier. Absent when the ledger read failed |
| Checks qualifier " · recorded, unchecked" / " · checked: `<criteria>`" / " · declared, not evaluated: `<criteria>`" | `components/layout/RightPanel.tsx` `FiringLines` | the routine's declared `criteria` (immutable after save) against the signed envelope's `verdicts[]` (`firing_view.checks_view`) | **added in story 2.5** — "checked" only where each declared criterion's verdict is present in the signed envelope and compared something (a `counts_match` verdict that compared nothing reads "declared, not evaluated"). No criteria → "recorded, unchecked". Absent when no envelope was read. Never "verified" |
| Receipt control "Receipt" / "receipt in the owner's session" | `components/layout/RightPanel.tsx` `FiringLines` | an `attestations` row for the firing's `run_id` in the firing's own session (`receipt_available`); the viewer owning that session (`receipt_downloadable`) | **added in story 2.5** — the button saves the stored envelope bytes via `lib/downloadReceipt.ts`; a failed download shows its error inline. An office owner viewing a member's firing sees the static text, because the receipt route lets only the session owner through. Absent when no envelope is stored |
| Mailer receipt "Settlement: `<settlement label>`" | `services/superagent/.../system_tools/mailer.py` `_render_receipt` (not frontend; listed so the map is whole) | `audit.settlement.label` from the same export | **added in story 2.6** — a line after the summary only when `settlement` is present. The summary adds ", N not checked" for unchecked steps, which are no longer counted as verified |
| Workflows page "Last run" | `pages/Workflows.tsx` `WorkflowCard` | the last firing's server label (same as the pane) and its slot | **relabelled in story 2.5** — was the template's `updated_at`, which no run produced; now "`<label>` · `<relative slot time>`", or "—" with no firing. A `scheduled` template now reads the plain word "scheduled" (it was the "Running" badge) |
| Export coverage block `coverage` (statement + "The signed receipt for run … has N step(s). This export's step list is the session's transcript …, not itself signed.") | `GET /sessions/{id}/audit` (not frontend; listed so the map is whole) | the SDK verifier's `coverage_statement`, computed from the run's sealed envelope at export time, never stored | **added in story 3.2** (FR-8) — in every export, chat and firing alike, including one with no receipt ("No signed receipt was read for this export.") and a deployment without the SDK (a pinned copy of the same text, and "could not check its signature"). "The signed receipt" is said only after the stored envelope verified offline at export time; one that does not verify reads "A stored receipt for run … did not verify" with no step count, tools or `models` read from it. `orcha verify` prints the same statement as a "Coverage:" block |
| Per-turn model caption " · `<route>/<model id>`" | `components/chat/MessageBubble.tsx` (the timestamp line of an assistant message) | the server's `turn_model` ("<route>/<model id>": the id the provider says it served, and where the request went — the endpoint's host name, or `local` for a loopback, private-network or link-local address; `local` is the first hop only and is never inferred from a port) on the SSE `done` event, or the assistant transcript row's `tool_inputs.model`; signed into the receipt as the `model` verdict (AD-21) when the turn sealed one — the signed model is the one that requested the turn's steps; the caption and row show the turn's last LLM call, which a provider alias may have served with another id | **added in story 3.3** (FR-13) — rendered as is, never derived from the picker: a turn whose model the server did not record shows nothing. The ModelChip's "hosted" / BYOK label is the setting, not the record |
| Export `models` | `GET /sessions/{id}/audit` (not frontend; listed so the map is whole) | the sealed run's signed `model` verdicts, read back from the envelope | **added in story 3.3** — absent when no receipt was read; `[]` for a receipt sealed before the verdict existed |
| Mailer summary "structurally checked" / "failed" / "not checked" per step, and "What a signed receipt covers:" | `services/superagent/.../system_tools/mailer.py` `_render_receipt` | structural check per step; `audit.coverage` | **relabelled in story 3.2** — was "verified" / "failed"; "failed" stays bare because the export does not say whether the structural check failed or never ran (an errored or refused call); the mail is a "run summary" (it is not the signed receipt), says its own turn's receipt seals after it, and names Metaorcha in its sender and subject |

Recorded, not changed here:

- Audit package fields `summary.steps_verified` / `steps_failed` describe the
  structural check, not the gate (story 2.6 stopped counting unchecked steps as
  verified; the field names stay, since renaming them breaks API readers). The
  mailer and every UI label render them as "structurally checked" / "failed".
- No string describes the record as covering all of the agent's work:
  `common/utils/tests/test_record_wording.py` sweeps the tree for the phrases
  it forbids (README is frozen and checked when it next changes).
