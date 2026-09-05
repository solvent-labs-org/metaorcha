#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
# KYA settlement slice — Story 3.1, the dual-run demo.
#
# One live stack, two legs, opposite outcomes. The point is not that settling
# works; it is that refusing works, and that the refusal names the broken link
# precisely enough for a third party to check it.
#
#   LEG 1  settle  — a real charged agent call is attested, verified, and
#                    settled. `attested_settlements` gains one row with
#                    outcome=settled, settled_run_id=<run>, failed_checks=[].
#
#   LEG 2  refuse  — the same envelope with one byte of one step changed is
#                    replayed under a fresh run_id. The gate refuses, writes
#                    no credit, and names exactly one failed check:
#                    ["steps_root"] — the link that actually broke, and not
#                    the three that never ran.
#
# That last assertion is the whole demo. Before story 2.2a the gate reported a
# steps_root failure as steps_root AND signature AND schema, because the
# verifier returns at the first failure and leaves later checks at the False
# they were initialised with. An audit row naming checks that never executed
# is not naming the failed check. This script fails if more than one comes back.
#
# Usage:
#   ./scripts/run-all.sh --skip-seed      # bring the stack up first
#   ./scripts/kya-settlement-slice.sh
#
#   KYA_SLICE_KEEP=1 ./scripts/kya-settlement-slice.sh   # keep the probe agent up
#
#   # A port already owned by something else: start the four services by hand
#   # on other ports and point the slice at them. Every port below is derived
#   # from these URLs; nothing is hardcoded further down.
#   GATEWAY_URL=http://localhost:8081 ./scripts/kya-settlement-slice.sh
#   (also REGISTRY_URL, PND_URL, SUPERAGENT_URL)
#
# Requires: registry, PnD, superagent and gateway up (defaults
# :8000/:8001/:8002/:8080, overridable as above), a superagent booted with
# RUN_ATTESTATION_ENABLED, SETTLEMENT_REQUIRE_ATTESTATION and
# RUN_ATTESTATION_CHARTER_HASH set, and Postgres carrying the lane tables with
# every migration on this line applied (`attested_settlements.call_id`).
# Agent registration needs the registry booted with DISABLE_AUTH=true (the
# local path in docs/setup.md) or ORCHA_PAT exported for `emerge publish`.
#
# Do not reach for run-all.sh to "free" a port: its kill_port sweep takes
# twelve ports with no ownership check, and on a shared machine those ports
# belong to other projects.
# Every precondition is checked before either leg runs — a demo that reports
# green because a flag was off is worse than one that does not run.
# ═══════════════════════════════════════════════════════════════════════════
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
GATEWAY="${GATEWAY_URL:-http://localhost:8080}"
REGISTRY="${REGISTRY_URL:-http://localhost:8000}"
PND="${PND_URL:-http://localhost:8001}"
SUPERAGENT="${SUPERAGENT_URL:-http://localhost:8002}"
AGENT_HOST="${KYA_AGENT_HOST:-127.0.0.1}"
SSE_TIMEOUT="${KYA_SSE_TIMEOUT:-240}"
PROBE_DID="did:orcha:agent:poc-probe"

# The charter the settling path binds to. Not sha256sum of the file — it is
# sha256_hex(canonical_json_bytes(...)), pinned in
# services/validator/tests/test_charter_fixture.py.
CHARTER_FIXTURE="$ROOT/docs/spec/fixtures/demo-charter.json"

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
EVIDENCE="${KYA_SLICE_EVIDENCE:-$ROOT/.kya-evidence/$STAMP}"
mkdir -p "$EVIDENCE"

AGENT_PID=""
TMP="$(mktemp -d)"
cleanup() {
  if [[ -n "$AGENT_PID" && -z "${KYA_SLICE_KEEP:-}" ]]; then
    kill "$AGENT_PID" 2>/dev/null
  fi
  rm -rf "$TMP"
}
trap cleanup EXIT

BOLD=$'\033[1m'; RESET=$'\033[0m'
GREEN=$'\033[0;32m'; RED=$'\033[0;31m'; YELLOW=$'\033[0;33m'; CYAN=$'\033[0;36m'

say()  { echo; echo "${BOLD}━━ $1${RESET}"; }
ok()   { echo "  ${GREEN}✅${RESET} $1"; }
bad()  { echo "  ${RED}❌${RESET} $1"; }
info() { echo "  ${CYAN}ℹ${RESET}  $1"; }
warn() { echo "  ${YELLOW}⚠${RESET}  $1"; }

FAILED=0
fail() { bad "$1"; FAILED=1; }

# PYTHONPATH: validator and emerge_node are workspace packages with no editable
# install (see the dev fix on this branch that puts them on superagent's path).
PYPATH="$ROOT/services/validator/src:$ROOT/node/src:$ROOT/sdk/src:$ROOT/scripts"

# DATABASE_URL is resolved from the RUNNING superagent, not from any .env file
# (set in stage 0 below). Six env files in this tree name a database and they
# have disagreed before; the only one that matters is the one the service whose
# gate we are exercising actually holds open. Asserting against a different
# database than the service writes to is the failure mode this avoids.
SLICE_DB_URL=""
# The assertion process imports the superagent's own config module, which
# builds its Settings at import and requires a handful of fields. In a
# checkout with no services/superagent/.env that import fails before a single
# assertion runs. Give the process the RUNNING service's values for exactly
# those fields — read from /proc in stage 0, never from a file — so the
# assertions see the same configuration the service settled under.
SA_ENV=()
py() { PYTHONPATH="$PYPATH" DATABASE_URL="$SLICE_DB_URL" env "${SA_ENV[@]}" uv run --project "$ROOT" python "$@"; }

# ─── Stage 0 — preconditions ────────────────────────────────────────────────
say "Stage 0 — preconditions"

# Identity, not liveness. A foreign process on one of these ports can answer
# 200 {"status":"ok"} on /health (a local private-gpt did exactly that on
# :8080), and a liveness probe would then run the slice against it. Each of
# our services publishes its own title in /openapi.json; assert that.
identity_of() { curl -sf --max-time 5 "$1/openapi.json" 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin).get("info",{}).get("title",""))' 2>/dev/null || true; }
for spec in "$GATEWAY|Orcha Gateway" "$REGISTRY|Orcha Registry Service" "$PND|Orcha Planning & Discovery Service" "$SUPERAGENT|Orcha SuperAgent"; do
  base="${spec%%|*}"; want="${spec#*|}"
  got="$(identity_of "$base")"
  if [[ "$got" == "$want" ]]; then
    ok "up: $base is \"$want\""
  elif [[ -n "$got" ]]; then
    fail "$base answers as \"$got\", not \"$want\" — a foreign process holds that port; free it, then ./scripts/run-all.sh --skip-seed"
  elif curl -s --max-time 5 -o /dev/null "$base/" 2>/dev/null; then
    fail "$base answers but publishes no identity — not \"$want\"; free the port, then ./scripts/run-all.sh --skip-seed"
  else
    fail "down: $base — run ./scripts/run-all.sh --skip-seed first"
  fi
done
[[ $FAILED -eq 1 ]] && { echo; bad "stack not ready — aborting before either leg"; exit 1; }

# The flags are read at boot, so the file on disk is not evidence. Read the
# running process's own environment instead.
SA_PID="$(pgrep -f 'uvicorn superagent.main:app' | head -1 || true)"
if [[ -z "$SA_PID" ]]; then
  fail "cannot find the superagent process — flags unverifiable"
else
  ok "superagent pid $SA_PID"
  sa_env() { tr '\0' '\n' < "/proc/$SA_PID/environ" 2>/dev/null | grep "^$1=" | cut -d= -f2-; }
  for flag in RUN_ATTESTATION_ENABLED SETTLEMENT_REQUIRE_ATTESTATION; do
    v="$(sa_env "$flag")"
    if [[ "${v,,}" == "true" ]]; then
      ok "$flag=true (in the running process)"
    else
      fail "$flag=${v:-<unset>} — the gate is not armed; this demo would prove nothing"
    fi
  done
  CHARTER_ENV="$(sa_env RUN_ATTESTATION_CHARTER_HASH)"
  if [[ -n "$CHARTER_ENV" ]]; then
    ok "RUN_ATTESTATION_CHARTER_HASH set (${CHARTER_ENV:0:12}…)"
  else
    fail "RUN_ATTESTATION_CHARTER_HASH unset — every gated settle refuses on charter_hash"
  fi
  # The DB the process actually talks to, not the one an .env file names.
  SLICE_DB_URL="$(sa_env DATABASE_URL)"
  for key in OPENROUTER_API_KEY REDIS_URL VAULT_KEY PND_SERVICE_URL \
             RUN_ATTESTATION_ENABLED SETTLEMENT_REQUIRE_ATTESTATION RUN_ATTESTATION_CHARTER_HASH; do
    v="$(sa_env "$key")"; [[ -n "$v" ]] && SA_ENV+=("$key=$v")
  done
  SA_DB="$(printf '%s' "$SLICE_DB_URL" | sed -E 's#.*/([^/?]+)(\?.*)?$#\1#')"
  if [[ -n "$SLICE_DB_URL" ]]; then
    ok "asserting against the superagent's own database: ${SA_DB}"
  else
    fail "superagent has no DATABASE_URL — cannot assert against the DB it writes to"
  fi
fi

# Record the machine state this package was produced on. The extract-readiness
# checklist (Story 3.3) reads it back: a package that does not say which
# commit, which host, whether the tree was clean and which payment mode the
# gateway ran in is not evidence of a clean-machine, mock-only run.
# Find the gateway process by the port GATEWAY_URL names (never a literal
# 8080 — with the gateway moved to another port, a literal would find nothing,
# or a foreign process) and read its PAYMENT_MODE from /proc, not from a file.
GW_PORT="$(printf '%s' "$GATEWAY" | sed -E 's#^[a-z]+://[^:/]+:?([0-9]*).*$#\1#')"
GW_PORT="${GW_PORT:-80}"
GW_PID="$(ss -ltnp 2>/dev/null | awk -v p=":${GW_PORT}" 'substr($4, length($4) - length(p) + 1) == p {print $NF}' | grep -oE 'pid=[0-9]+' | head -1 | cut -d= -f2 || true)"
GW_PAYMENT_MODE=""
if [[ -n "$GW_PID" ]]; then
  GW_PAYMENT_MODE="$(tr '\0' '\n' < "/proc/$GW_PID/environ" 2>/dev/null | grep '^PAYMENT_MODE=' | cut -d= -f2- || true)"
fi
python3 - "$EVIDENCE/environment.json" "${GW_PAYMENT_MODE:-}" "${SA_PID:-}" <<'PYENV'
import json, platform, subprocess, sys, time
out, payment_mode, sa_pid = sys.argv[1:4]
def git(*a):
    return subprocess.run(["git", *a], capture_output=True, text=True).stdout.strip()
flags = {}
if sa_pid:
    try:
        for kv in open(f"/proc/{sa_pid}/environ", "rb").read().split(b"\0"):
            k, _, v = kv.partition(b"=")
            if k in (b"RUN_ATTESTATION_ENABLED", b"SETTLEMENT_REQUIRE_ATTESTATION",
                     b"RUN_ATTESTATION_CHARTER_HASH", b"PAYMENT_MODE"):
                flags[k.decode()] = v.decode(errors="replace")
    except OSError:
        pass
record = {
    "when": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    "host": platform.node(),
    "kernel": platform.release(),
    "head": git("rev-parse", "HEAD"),
    "dirty": bool(git("status", "--porcelain")),
    "payment_mode": payment_mode or None,  # the gateway's own, read from /proc
    "superagent_flags": flags,
}
with open(out, "w") as fh:
    json.dump(record, fh, indent=2, sort_keys=True)
PYENV
ok "environment recorded: $EVIDENCE/environment.json (payment_mode=${GW_PAYMENT_MODE:-<unrecorded>})"

# The configured charter must be the fixture's canonical hash, or leg 1 refuses
# for a reason that has nothing to do with what the demo is showing.
if [[ -f "$CHARTER_FIXTURE" ]]; then
  CHARTER_COMPUTED="$(py - "$CHARTER_FIXTURE" <<'PY' 2>/dev/null
import json, sys
from emerge_node.envelope import canonical_json_bytes
from validator.run_envelope import sha256_hex
print(sha256_hex(canonical_json_bytes(json.load(open(sys.argv[1])))))
PY
)"
  if [[ "$CHARTER_COMPUTED" == "$CHARTER_ENV" ]]; then
    ok "charter binds to the demo fixture (canonical-JSON hash, not file bytes)"
  else
    fail "charter mismatch: process has ${CHARTER_ENV:0:12}…, fixture hashes to ${CHARTER_COMPUTED:0:12}…"
  fi
else
  fail "charter fixture missing: $CHARTER_FIXTURE"
fi

# Lane tables. Their absence is the R8 blocker and produces a confusing failure
# much later, so name it here.
DB_PROBE="$(py - 2>"$TMP/dbprobe.err" <<'PYEOF'
import asyncio
from src.generated_client import Prisma


async def main():
    db = Prisma()
    try:
        await db.connect()
    except Exception:
        print("NO_CONNECT")
        return
    try:
        for table in ("attestations", "attested_settlements"):
            try:
                await db.query_raw(f"SELECT 1 FROM {table} LIMIT 1")
            except Exception:
                print(f"NO_TABLE:{table}")
                return
        # The newest column on this line; its absence means a pending
        # migration and would surface as a column error inside leg 1.
        try:
            await db.query_raw("SELECT call_id, settled_run_id FROM attested_settlements LIMIT 1")
        except Exception:
            print("NO_COLUMN:attested_settlements.call_id")
            return
        print("OK")
    finally:
        await db.disconnect()


asyncio.run(main())
PYEOF
)"
case "$DB_PROBE" in
  OK) ok "lane tables present (attestations, attested_settlements)" ;;
  NO_CONNECT) fail "cannot reach the database at ${SA_DB:-<unknown>} — is Postgres up? (see $TMP/dbprobe.err)" ;;
  NO_TABLE:*) fail "missing table ${DB_PROBE#NO_TABLE:} — run: uv run prisma migrate deploy --schema common/database/schema.prisma" ;;
  NO_COLUMN:*) fail "missing column ${DB_PROBE#NO_COLUMN:} — a migration on this line is pending: uv run prisma migrate deploy --schema common/database/schema.prisma" ;;
  *) fail "database probe failed: ${DB_PROBE:-<no output>} (see $TMP/dbprobe.err)" ;;
esac

[[ $FAILED -eq 1 ]] && { echo; bad "preconditions failed — aborting before either leg"; exit 1; }

# ─── Stage 1 — publish the probe agent ──────────────────────────────────────
say "Stage 1 — publish the paid A2A probe agent"

PYTHONPATH="$ROOT/sdk/src" python3 "$ROOT/agents/poc-probe-agent/agent.py" \
  > "$TMP/agent.log" 2>&1 &
AGENT_PID=$!
PROBE_UP=0
for _ in $(seq 1 20); do
  sleep 0.5
  curl -sf --max-time 2 "http://127.0.0.1:8930/.well-known/agent.json" >/dev/null 2>&1 && { PROBE_UP=1; break; }
done
[[ $PROBE_UP -eq 1 ]] && ok "probe agent listening" || warn "probe agent health unconfirmed (see $TMP/agent.log)"

# Force a fresh registration. A DID left registered across day-gaps is flipped
# UNHEALTHY by the registry HealthMonitor and PnD's GIN pre-filter then drops it
# from discovery — the run would fail with no agent and no useful reason.
curl -sf -X DELETE "$REGISTRY/api/v1/agents/$PROBE_DID" >/dev/null 2>&1 \
  && ok "prior registration soft-deleted (fresh health probe forced)" \
  || info "no prior registration to delete"

if PYTHONPATH="$ROOT/sdk/src" python3 -m emerge.cli publish \
    "$ROOT/agents/poc-probe-agent/agent.py" \
    --registry "$REGISTRY" --host "$AGENT_HOST" > "$TMP/publish.log" 2>&1; then
  ok "emerge publish → registry"
elif curl -sf "$REGISTRY/api/v1/agents?limit=100" | grep -q "$PROBE_DID"; then
  ok "publish returned non-zero but $PROBE_DID is registered"
else
  fail "emerge publish failed — $(tail -1 "$TMP/publish.log")"
fi

# Discovery needs the manifest embedded; publish alone does not do this.
if curl -sf -X POST "$PND/api/v1/manifests/process" \
     -H 'Content-Type: application/json' -d "{\"agent_id\":\"$PROBE_DID\"}" 2>/dev/null \
   | python3 -c "import json,sys; sys.exit(0 if json.load(sys.stdin).get('success') else 1)" 2>/dev/null; then
  ok "PnD embeddings processed"
else
  fail "PnD manifests/process failed — the probe will not be discoverable"
fi

[[ $FAILED -eq 1 ]] && { echo; bad "could not publish the probe agent — aborting"; exit 1; }

# ─── Stage 2 — LEG 1: a charged run settles ─────────────────────────────────
say "Stage 2 — LEG 1 (settle): a charged call is attested, verified, settled"

EMAIL="kya-slice-$(date +%s)@example.com"
TOKEN="$(curl -sf -X POST "$GATEWAY/auth/register" \
  -H 'Content-Type: application/json' \
  -d "{\"email\":\"$EMAIL\",\"password\":\"kya-slice-pass-123\",\"display_name\":\"KYA Slice\"}" 2>/dev/null |
  python3 -c "import json,sys; print(json.load(sys.stdin)['access_token'])" 2>/dev/null)" || TOKEN=""
[[ -z "$TOKEN" ]] && { fail "auth register failed"; exit 1; }
ok "registered $EMAIL"

SESSION_ID="$(curl -sf -X POST "$GATEWAY/api/v1/sessions" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -d '{}' 2>/dev/null |
  python3 -c "import json,sys; print(json.load(sys.stdin)['session_id'])" 2>/dev/null)" || SESSION_ID=""
[[ -z "$SESSION_ID" ]] && { fail "could not create a session"; exit 1; }
ok "session $SESSION_ID"

GOAL="Run a system probe report for hello-world"
info "goal: $GOAL"
curl -sf -N -X POST "$GATEWAY/api/v1/sessions/$SESSION_ID/message" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d "{\"message\":\"$GOAL\"}" --max-time "$SSE_TIMEOUT" > "$EVIDENCE/leg1-sse.txt" 2>/dev/null || true

if [[ ! -s "$EVIDENCE/leg1-sse.txt" ]]; then
  fail "no SSE output — the run did not start"
  exit 1
fi

# emit_run_complete is awaited before the done event, so a done event means the
# attestation observer has sealed and the gate observer has replayed the
# deferred settle. Waiting for done is sufficient; nothing else needs polling.
if grep -q '"type": *"done"\|"type":"done"' "$EVIDENCE/leg1-sse.txt"; then
  ok "run reached the done event (settle already replayed)"
else
  warn "no done event — the run may have been cut short by the SSE timeout"
fi

if python3 - "$EVIDENCE/leg1-sse.txt" <<'PY'
import json, sys
events = []
for line in open(sys.argv[1], errors="replace"):
    line = line.strip()
    if not line.startswith("data:"):
        continue
    p = line[5:].strip()
    if not p or p == "[DONE]":
        continue
    try:
        events.append(json.loads(p))
    except json.JSONDecodeError:
        pass
res = [e for e in events if e.get("type") == "invocation_result"
       and "poc-probe" in str(e.get("agent_id", ""))]
assert res, "no invocation_result for poc-probe — nothing was charged, so nothing can settle"
print("ok")
PY
then
  ok "the paid A2A probe was invoked (there is a charge to settle)"
else
  fail "the probe was never invoked — see $EVIDENCE/leg1-sse.txt"
fi

[[ $FAILED -eq 1 ]] && { echo; bad "leg 1 did not produce a charged call — aborting"; exit 1; }

# ─── Stage 3 — LEG 1 assertions + LEG 2, both against the live database ─────
say "Stage 3 — LEG 1 assertions, then LEG 2 (refuse) on a tampered replay"

py - "$SESSION_ID" "$EVIDENCE" "$ROOT" <<'PY'
import asyncio, json, subprocess, sys, uuid

SESSION_ID, EVIDENCE, ROOT = sys.argv[1], sys.argv[2], sys.argv[3]

GREEN, RED, CYAN, RESET = "\033[0;32m", "\033[0;31m", "\033[0;36m", "\033[0m"
def ok(m):   print(f"  {GREEN}✅{RESET} {m}")
def bad(m):  print(f"  {RED}❌{RESET} {m}")
def info(m): print(f"  {CYAN}ℹ{RESET}  {m}")

failed = []
evidence = {"session_id": SESSION_ID}


async def main() -> int:
    from src.generated_client import Prisma
    from superagent.config import settings
    from superagent.pricing.settle_gate import gate_attested_settle
    from validator.run_envelope import persist_run_attestation

    db = Prisma()
    await db.connect()
    try:
        # ── LEG 1 ────────────────────────────────────────────────────────────
        att = await db.attestation.find_first(
            where={"session_id": SESSION_ID}, order={"created_at": "desc"}
        )
        if att is None:
            bad("no attestation row for this session — the run was never sealed")
            return 1
        run_id = att.run_id
        ok(f"envelope sealed and persisted for run {run_id}")
        evidence["leg1"] = {"run_id": run_id, "envelope_digest": att.case_hash}

        settled = await db.attestedsettlement.find_first(where={"settled_run_id": run_id})
        if settled is None:
            bad(f"no settled row claims run {run_id}")
            refused = await db.attestedsettlement.find_many(where={"run_id": run_id})
            for r in refused:
                bad(f"  gate refused instead: outcome={r.outcome} failed_checks={r.failed_checks}")
            if not refused:
                bad("  and no refused row either — the gate never ran for this run")
            failed.append("leg1_not_settled")
        else:
            ok(f"attested_settlements: outcome={settled.outcome}, settled_run_id={settled.settled_run_id}")
            checks = settled.failed_checks
            if isinstance(checks, str):
                checks = json.loads(checks)
            if checks:
                bad(f"a settled row carries failed_checks={checks} — it should be empty")
                failed.append("leg1_failed_checks_nonempty")
            else:
                ok("failed_checks is empty on the settled row")
            evidence["leg1"]["outcome"] = settled.outcome
            evidence["leg1"]["failed_checks"] = checks
            evidence["leg1"]["charter_hash"] = settled.charter_hash

            # Story 2.4 closed this: the claim, the debit and the Transaction
            # row commit in one transaction, so a settled row with no
            # Transaction row cannot be produced by a failure between them.
            # If it ever shows up here it is a regression in settlement.py,
            # not a fault in this script — say so precisely.
            txns = await db.transaction.find_many(where={"session_id": SESSION_ID})
            if txns:
                ok(f"credit written: {len(txns)} transaction row(s) for this session")
                evidence["leg1"]["transactions"] = len(txns)
            else:
                bad("SETTLED BUT UNBILLED — audit row claimed run "
                    f"{run_id} and no Transaction row exists.")
                bad("  Story 2.4 made this impossible by construction — treat it "
                    "as a regression in settlement.py, not a bug in this script.")
                bad("  The run is now permanently unsettleable: 2.3's unique index "
                    "on settled_run_id will refuse every retry.")
                failed.append("leg1_settled_but_unbilled")

        # ── LEG 2 ────────────────────────────────────────────────────────────
        print()
        info("LEG 2 — replaying the same envelope with one step's output_hash zeroed")

        envelope = getattr(att.payload, "data", att.payload)
        if isinstance(envelope, str):
            envelope = json.loads(envelope)
        tampered = json.loads(json.dumps(envelope))

        if not tampered.get("steps"):
            bad("the sealed envelope has no steps — nothing to tamper with")
            return 1

        # attestations.run_id is unique, so the replay needs its own id. The
        # gate compares envelope["run_id"] against the run_id argument BEFORE
        # verifying, so the embedded id has to be rewritten too — otherwise the
        # refusal is run_id_mismatch and the demo proves the wrong thing.
        # Rewriting it invalidates the signature, which is harmless here:
        # steps_root is checked before signature and fails first.
        replay_run_id = f"run_{uuid.uuid4().hex}"
        tampered["run_id"] = replay_run_id
        original_hash = tampered["steps"][0].get("output_hash")
        tampered["steps"][0]["output_hash"] = "0" * 64
        info(f"step[0].output_hash: {str(original_hash)[:16]}… → 0000…")

        persisted = await persist_run_attestation(SESSION_ID, tampered, db=db)
        if persisted is None:
            bad("could not persist the tampered envelope")
            return 1
        ok(f"tampered envelope persisted under {replay_run_id}")

        outcome = await gate_attested_settle(
            run_id=replay_run_id,
            session_id=SESSION_ID,
            expected_charter_hash=settings.run_attestation_charter_hash,
            db=db,
        )
        evidence["leg2"] = {"run_id": replay_run_id, **outcome}

        if outcome["outcome"] == "refused":
            ok("the gate REFUSED the tampered run")
        else:
            bad(f"the gate returned {outcome['outcome']!r} on a tampered envelope — fail-closed is broken")
            failed.append("leg2_not_refused")

        checks = outcome["failed_checks"]
        if checks == ["steps_root"]:
            ok("failed_checks == ['steps_root'] — exactly one check, and it is the one that broke")
        elif "steps_root" in checks:
            bad(f"failed_checks={checks} — steps_root is named, but so are "
                f"{len(checks) - 1} check(s) that never ran.")
            bad("  This is the pre-2.2a over-report. The branch is built on the wrong base.")
            failed.append("leg2_overreports")
        else:
            bad(f"failed_checks={checks} — expected ['steps_root']")
            failed.append("leg2_wrong_check")

        # A refusal must write no credit, and must not claim the run.
        claim = await db.attestedsettlement.find_first(where={"settled_run_id": replay_run_id})
        if claim is None:
            ok("the refused run holds no settled claim (settled_run_id stays NULL)")
        else:
            bad("a refused run claimed settled_run_id — the idempotency column is wrong")
            failed.append("leg2_claimed")

        row = await db.attestedsettlement.find_first(
            where={"run_id": replay_run_id}, order={"created_at": "desc"}
        )
        if row is not None and row.outcome == "refused":
            ok("the refusal is itself audited (outcome=refused row written)")
        else:
            bad("the refusal was not audited — a refuse that leaves no record is not fail-closed")
            failed.append("leg2_unaudited")
        # ── Evidence package (Story 3.2): envelopes + audit records + manifest ──
        # Written from the live rows, so the manifest says "dual-run". Offline
        # re-verify is `python scripts/kya_evidence_package.py verify <dir>`.
        from pathlib import Path
        from kya_evidence_package import write_package
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip() or None
        legs = []
        if settled is not None:
            legs.append((envelope, settled, "settled"))
        if row is not None:
            legs.append((tampered, row, "refused"))
        if len(legs) == 2:
            write_package(Path(EVIDENCE), source="dual-run", head=head, legs=legs)
            ok(f"evidence package written: {EVIDENCE}/manifest.json (source=dual-run)")
        else:
            bad(f"evidence package NOT written — only {len(legs)} of 2 legs have rows")
            failed.append("package_incomplete")
    finally:
        await db.disconnect()

    with open(f"{EVIDENCE}/slice.json", "w") as fh:
        json.dump(evidence, fh, indent=2, sort_keys=True)
    return 1 if failed else 0


sys.exit(asyncio.run(main()))
PY
[[ $? -ne 0 ]] && FAILED=1

# ─── Summary ────────────────────────────────────────────────────────────────
say "Result"
echo "  evidence: $EVIDENCE"
if [[ $FAILED -eq 0 ]]; then
  echo
  echo "  ${GREEN}${BOLD}KYA SETTLEMENT SLICE: PROVEN${RESET}"
  echo "  A valid run settled. A tampered run refused, wrote no credit, and named"
  echo "  exactly one broken link."
  exit 0
else
  echo
  echo "  ${RED}${BOLD}KYA SETTLEMENT SLICE: FAILED${RESET}"
  exit 1
fi
