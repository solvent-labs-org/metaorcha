# RFC 0003 — Run attestation envelope (KYA harness)

- **Status:** Accepted
- **Type:** Additive — new specification artifact; no emerge.yaml schema change
- **Workstream:** KYA (Know Your Agent) harness
- **Issue:** spec-change

## Motivation

Every SuperAgent run today produces an ephemeral trace: tool calls, verdicts,
and timings are observable in-process but leave behind no portable,
third-party-checkable evidence of *what the agent actually did*. Supervisors,
auditors, and counterparties need a **signed run attestation**: a single
envelope that binds a run's ordered tool-call transcript (as hashes), its
verdicts, and its timing to a signer's key, verifiable **offline for free** —
no network, no anchoring service, no shared secret.

This RFC defines that envelope. It reuses the canonicalisation and Ed25519
primitives already shared by signed manifest envelopes
(`node/src/emerge_node/envelope.py`), AAC charters
(`common/charter/src/charter/signing.py`), and validator case attestations
(`services/validator/src/validator/signer.py`), so one crypto implementation
covers every signed artifact in the platform.

Design constraints:

- **Privacy by design** — raw tool-call arguments and outputs are **never
  embedded** in the envelope; only their sha256 hashes appear. Raw content may
  be disclosed selectively, later, by the operator — the hashes let a verifier
  check any such disclosure against the attestation.
- **Offline verification** — verification needs only the envelope itself;
  DID resolution and charter lookup are strictly optional enrichments.
- **Byte-exact determinism** — two independent implementations given the same
  logical envelope MUST produce byte-identical canonical forms, digests, and
  chain roots.

## Definitions (normative)

- `canonical_json_bytes(value)` — the canonical JSON form shared across the
  platform, defined by `canonical_json_bytes` in
  `node/src/emerge_node/envelope.py`: the UTF-8 encoding of the JSON
  serialization of `value` with **object keys sorted** (by Unicode code
  point), **compact separators** (`","` and `":"`, no whitespace), and
  non-ASCII characters escaped as `\uXXXX` — i.e. exactly the bytes produced
  by Python `json.dumps(value, sort_keys=True, separators=(",",
  ":")).encode("utf-8")`. That function is the **normative reference**: any
  re-implementation MUST produce byte-identical output for the same logical
  value.
- **Charset rule** — every string-typed field of this envelope that is not
  already bound to a fixed alphabet (the hex digests, the RFC 3339
  timestamps, the base64 fields, the `result` enum, the `format` constant)
  MUST match `^[\x20-\x7e]+$`: printable ASCII, non-empty. Those fields are
  `run_id`, `policy_version`, `steps[].call_id`, `steps[].tool`,
  `verdicts[].check`, `verdicts[].detail` (which alone may be empty:
  `^[\x20-\x7e]*$`), `signer.did`, and every entry of `agent_dids` — for the
  DIDs the rule binds the method-specific part after `did:<method>:`. Every
  numeric field is an integer; the envelope
  carries no non-integer number. A producer holding an identifier outside
  the rule cannot produce a conformant envelope for that run and MUST fail
  the attestation rather than rewrite the identifier.
- `sha256(b)` — the raw 32-byte SHA-256 digest of byte string `b`.
- `sha256_hex(b)` — the lowercase hexadecimal SHA-256 digest of byte string
  `b` (64 hex characters).
- "Hex digest bytes" of a hex string `h` means the 64-byte ASCII encoding of
  `h` (`h.encode("ascii")`); hex characters are ASCII, so ASCII and UTF-8
  encodings coincide.

### Canonical form and RFC 8785

The normative canonical form is the Python rendering above. The charset rule
exists so that a second, independently specified canonical form reaches the
same bytes: RFC 8785, the JSON Canonicalization Scheme (JCS). The two
renderings differ in exactly three places. JCS leaves characters outside
ASCII as raw UTF-8 where the reference escapes them as `\uXXXX` (and JCS
leaves `DEL`, U+007F, unescaped where the reference escapes it); JCS sorts
object keys by UTF-16 code unit where the reference sorts by code point; and
JCS renders numbers per ECMAScript `Number::toString` where the reference
uses CPython's `repr`. None of the three can arise inside a conformant
envelope: every string is printable ASCII, every object key is a fixed ASCII
name, and every number is a small integer (a sequence index, a millisecond
latency, a basis-point score), which both render as plain decimal digits.
Neither form emits whitespace, and both escape `"`, `\` and the control
characters identically.

Therefore **core envelope verification — the chain root, the Merkle root, the
signed digest and the signature — is byte-identical under RFC 8785 and under
the Python reference.** A verifier MAY canonicalise each `steps[i]` and the
unsigned envelope with a JCS implementation and reach the same `s_i`,
`steps_root`, `steps_merkle_root` and `digest` as the reference. The test
vectors in `docs/spec/test-vectors/` are checked against both renderings.

The claim stops at the envelope boundary. `args_hash` and `output_hash` are
computed over the **raw** `args` and `output`, which are arbitrary agent JSON:
they may contain text outside ASCII and non-integer numbers, and for those
the two forms diverge. A verifier that re-hashes a selectively disclosed
`args` or `output` against the envelope MUST render it with the reference
form — sorted keys, compact separators, `\uXXXX` escaping of every character
outside printable ASCII, CPython float formatting — and MUST NOT assume a JCS
rendering reproduces the hash. This RFC claims parity for the envelope, not
for the content the envelope commits to.

## Envelope format

The format identifier is the string `"orcha.run-attestation/v1"`. Any future
breaking change to this envelope bumps the version suffix (`v1` → `v2`);
additive optional fields do not.

A run attestation envelope is a JSON object with **exactly** these fields
(no additional properties):

| Field | Type | Description |
|---|---|---|
| `format` | string, const | `"orcha.run-attestation/v1"` |
| `run_id` | string | Producer-assigned run identifier (unique per signer). Printable ASCII (charset rule). |
| `agent_dids` | array of string | DIDs of the agent(s) that executed the run. Each MUST be a DID in general syntax, `^did:[a-z0-9]+:[\x20-\x7e]+$` — a lowercase method name, then a printable-ASCII method-specific part (charset rule). The platform profile (below) additionally requires `did:orcha:agent:*` / `did:orcha:system:*`. |
| `charter_hash` | string or null | sha256 hex of the AAC charter (RFC 0002, `common/charter`) the run was authorised under, or `null` if none. |
| `policy_version` | string | Identifier of the policy/ruleset version the run was evaluated against. Printable ASCII (charset rule). |
| `steps` | array of object | Ordered tool-call records (see below). |
| `steps_root` | string | Linear chain root over `steps` (see "Step commitments"); 64 lowercase hex chars. |
| `steps_merkle_root` | string | RFC 6962 Merkle Tree Hash over the same canonical step bytes (see "Step commitments"); 64 lowercase hex chars. |
| `verdicts` | array of object | Verdicts produced by the run's evaluators (see below). Empty array allowed. |
| `started_at` | string | Dispatch time of the run's first step (first step's completion minus its `latency_ms`) — the step span, not wall-clock run start. RFC 3339 UTC with `Z` designator (e.g. `2026-08-06T00:58:21Z`). |
| `finished_at` | string | Completion time of the run's last step, same format as `started_at`. |
| `signer` | object | `{did, public_key_b64}` — see below. |
| `signature` | string | base64 Ed25519 signature (see "Signing"). |

Each `steps[]` entry is an object with **exactly** these fields:

| Field | Type | Description |
|---|---|---|
| `seq` | integer ≥ 0 | Sequence index; starts at 0 and increments by 1 with no gaps. |
| `call_id` | string | Tool-call identifier, unique within the run. Printable ASCII (charset rule). |
| `tool` | string | Name of the invoked tool/capability. Printable ASCII (charset rule). |
| `args_hash` | string | `sha256_hex(canonical_json_bytes(args))` of the call's arguments (a JSON object, possibly empty). |
| `output_hash` | string | `sha256_hex(canonical_json_bytes(output))` of the call's output (any JSON value). |
| `success` | boolean | Whether the call completed without error. |
| `latency_ms` | integer ≥ 0 | Wall-clock latency of the call in milliseconds. |
| `cdv_bp` | integer, optional | CDV Channel-A step-verification score in basis points, `0`–`1000`: the `cdv` package's `[0.0, 1.0]` score multiplied by 1000, rounded to the nearest integer and clamped (flag-gated step verification). Omitted entirely when step verification is disabled. An integer so that the envelope carries no non-integer number (see "Canonical form and RFC 8785"). |

Each `verdicts[]` entry is an object with **exactly** these fields:

| Field | Type | Description |
|---|---|---|
| `check` | string | Identifier of the check that produced the verdict (e.g. `authorized_scope`). Printable ASCII (charset rule). |
| `result` | string enum | One of `"pass"`, `"fail"`, `"warn"`. |
| `detail` | string, optional | Human-readable elaboration. Omitted when absent. Printable ASCII, may be empty (charset rule). |

`signer` is an object with exactly two fields:

- `did` — a DID in general syntax, `^did:[a-z0-9]+:[\x20-\x7e]+$` (charset
  rule). The platform profile requires `did:orcha:system:*` for platform
  signers such as the validator; an external implementer signs under its own
  method (`did:web:`, `did:key:`, …).
- `public_key_b64` — base64 of the 32-byte raw Ed25519 public key
  (same encoding as `generate_keypair()` in `envelope.py`).

### Platform profile (enforced by the platform, not by the schema)

The envelope schema binds DID *syntax*; it does not bind the DID *method*.
The Metaorcha platform applies a stricter profile on top of the schema:

- every entry of `agent_dids` matches `did:orcha:agent:*` or
  `did:orcha:system:*` (the AGENTS.md DID namespace rules), and
- `signer.did` is the configured platform signer, a `did:orcha:system:*` DID.

The profile is enforced where the platform produces envelopes (the producer
refuses any other DID) and where it settles them (the settlement gate refuses
an envelope signed by any other DID, and one whose `agent_dids` fall outside
the profile — each as its own named check, never as a schema failure).
An offline verifier checks the schema only. An external implementer signing
under `did:web:` or `did:key:` therefore emits a conformant `v1` envelope
without needing a `v2`; it does not meet the platform profile, which is a
platform decision, not a format one.

JSON Schema (informative; the prose above is normative):

```json
{
  "type": "object",
  "additionalProperties": false,
  "required": [
    "format", "run_id", "agent_dids", "charter_hash", "policy_version",
    "steps", "steps_root", "steps_merkle_root", "verdicts",
    "started_at", "finished_at",
    "signer", "signature"
  ],
  "properties": {
    "format": { "const": "orcha.run-attestation/v1" },
    "run_id": { "type": "string", "pattern": "^[\\x20-\\x7e]+$" },
    "agent_dids": {
      "type": "array",
      "items": { "type": "string", "pattern": "^did:[a-z0-9]+:[\\x20-\\x7e]+$" }
    },
    "charter_hash": { "type": ["string", "null"], "pattern": "^[0-9a-f]{64}$" },
    "policy_version": { "type": "string", "pattern": "^[\\x20-\\x7e]+$" },
    "steps": {
      "type": "array",
      "items": {
        "type": "object",
        "additionalProperties": false,
        "required": ["seq", "call_id", "tool", "args_hash", "output_hash", "success", "latency_ms"],
        "properties": {
          "seq": { "type": "integer", "minimum": 0 },
          "call_id": { "type": "string", "pattern": "^[\\x20-\\x7e]+$" },
          "tool": { "type": "string", "pattern": "^[\\x20-\\x7e]+$" },
          "args_hash": { "type": "string", "pattern": "^[0-9a-f]{64}$" },
          "output_hash": { "type": "string", "pattern": "^[0-9a-f]{64}$" },
          "success": { "type": "boolean" },
          "latency_ms": { "type": "integer", "minimum": 0 },
          "cdv_bp": { "type": "integer", "minimum": 0, "maximum": 1000 }
        }
      }
    },
    "steps_root": { "type": "string", "pattern": "^[0-9a-f]{64}$" },
    "steps_merkle_root": { "type": "string", "pattern": "^[0-9a-f]{64}$" },
    "verdicts": {
      "type": "array",
      "items": {
        "type": "object",
        "additionalProperties": false,
        "required": ["check", "result"],
        "properties": {
          "check": { "type": "string", "pattern": "^[\\x20-\\x7e]+$" },
          "result": { "enum": ["pass", "fail", "warn"] },
          "detail": { "type": "string", "pattern": "^[\\x20-\\x7e]*$" }
        }
      }
    },
    "started_at": { "type": "string", "format": "date-time", "pattern": "^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$" },
    "finished_at": { "type": "string", "format": "date-time", "pattern": "^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$" },
    "signer": {
      "type": "object",
      "additionalProperties": false,
      "required": ["did", "public_key_b64"],
      "properties": {
        "did": { "type": "string", "pattern": "^did:[a-z0-9]+:[\\x20-\\x7e]+$" },
        "public_key_b64": { "type": "string", "contentEncoding": "base64", "pattern": "^[A-Za-z0-9+/]+={0,2}$" }
      }
    },
    "signature": { "type": "string", "contentEncoding": "base64", "pattern": "^[A-Za-z0-9+/]+={0,2}$" }
  }
}
```

## Step commitments

The steps are bound by **two independent commitments over the same canonical
step bytes**. Both are required fields of a `v1` envelope, both are recomputed
by every verifier, and a mismatch in either is a hard failure.

They answer different questions and neither subsumes the other:

- `steps_root` — a linear hash chain, binding **order and completeness**. No
  step can be removed, reordered, or altered without changing the root.
- `steps_merkle_root` — a Merkle tree, binding **membership with position**.
  A single step can be shown to belong to the attested run by disclosing that
  step and `⌈log₂ n⌉` sibling digests, and nothing else. This is what makes
  the selective disclosure described in "Motivation" checkable: under the
  chain alone, verifying any one step requires disclosing every step.

Let `s_i = canonical_json_bytes(steps[i])` for the `n` steps of the run
(0-indexed). Both constructions are defined over exactly those byte strings.
All internal digests are **raw 32-byte values**; hexadecimal appears only at
the JSON boundary, in the two root fields.

### `steps_root` — linear chain

- `h_0 = sha256(s_0)`
- `h_i = sha256( h_{i-1} || s_i )` for `i ≥ 1`, where `h_{i-1}` is the **32
  raw bytes** of the previous digest and `||` is byte concatenation — i.e. the
  hash input is exactly `32 + len(s_i)` bytes: the previous digest as 32 raw
  bytes, immediately followed by the canonical step bytes.
- `steps_root` is the lowercase hexadecimal rendering of `h_{n-1}`.
- **Empty run** (`n = 0`): `steps_root = sha256_hex(b"")` =
  `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855`.

The chain links on **raw digest bytes, never on their hexadecimal rendering**.
An implementation that concatenates the 64-character hex string produces a
different root and is non-conformant.

### `steps_merkle_root` — Merkle Tree Hash

The construction is the Merkle Tree Hash (MTH) of **RFC 6962 §2.1**
(Certificate Transparency), unmodified, with SHA-256 and the same `s_i` as
leaf inputs:

- **Empty tree** — `MTH({}) = sha256(b"")`.
- **Single leaf** — `MTH({s_0}) = sha256( 0x00 || s_0 )`.
- **`n > 1`** — let `k` be the largest power of two **strictly less than `n`**;
  then `MTH(D[0:n]) = sha256( 0x01 || MTH(D[0:k]) || MTH(D[k:n]) )`.
- `steps_merkle_root` is the lowercase hexadecimal rendering of
  `MTH({s_0, …, s_{n-1}})`.

Three clauses there are load-bearing, and are written out rather than left to
the citation:

- The `0x00` leaf prefix and `0x01` node prefix are **domain separation**.
  Without them a leaf digest can be reinterpreted as an internal node, which
  is the standard second-preimage attack on a naive Merkle tree.
- The split is the largest power of two **strictly less than `n`**. The other
  common convention — duplicating a lone right-hand node to pad the level to a
  power of two — admits two distinct step lists with the same root. RFC 6962
  promotes the odd node instead and does not have that defect.
- For `n = 0` the two roots are numerically identical, because both reduce to
  `sha256(b"")`. That is a coincidence of the empty case. For `n ≥ 1` they are
  independent values and MUST NOT be assumed equal, or derived from each
  other.

Both roots are part of the signed payload (they are **not** excluded like the
charter's self-referential hash fields, because they commit to `steps`, not to
any field added by signing).

## Signing

Signing follows the AAC charter convention
(`common/charter/src/charter/signing.py`): **the signature is over the hex
digest, not over the canonical bytes directly.**

1. `unsigned` := the envelope object with all fields present **except**
   `signature`.
2. `digest` := `sha256_hex(canonical_json_bytes(unsigned))`.
3. `signature` := base64 of `Ed25519_sign(private_key,
   digest.encode("utf-8"))` — the message is the 64-byte UTF-8/ASCII encoding
   of the lowercase hex digest string.
4. Attach `signature` to the envelope.

Verification uses the shared `verify_bytes(message, signature_b64,
public_key_b64)` from `node/src/emerge_node/envelope.py` with `message =
digest.encode("utf-8")` — the same call path as charter and case-attestation
verification.

## Verification algorithm

A verifier performs, fully offline:

1. **Schema check** — the envelope parses as JSON; `format` equals
   `"orcha.run-attestation/v1"`; all required fields are present with the
   types and shapes defined above; no unknown fields; every DID matches the
   general DID pattern (the platform profile is not a schema check); all hash
   fields are 64
   lowercase hex characters; every free-text string satisfies the charset
   rule; `steps[].cdv_bp`, when present, is an integer in `[0, 1000]`;
   `steps[].seq` is 0-based and gapless.
2. **Recompute both commitments** — for each step recompute `s_i`, then
   compute the running chain `h_i` and the Merkle Tree Hash per the byte-level
   rules in "Step commitments".
3. **Compare both roots** — the recomputed chain root MUST equal the
   envelope's `steps_root`, and the recomputed Merkle Tree Hash MUST equal its
   `steps_merkle_root`. A mismatch in **either** means the transcript was
   tampered with. Neither root is advisory and neither may be skipped: a
   verifier that checks only one accepts envelopes whose two commitments
   describe different transcripts.
4. **Recompute the digest** — strip `signature` and recompute `digest =
   sha256_hex(canonical_json_bytes(unsigned))`.
5. **Verify the signature** — `verify_bytes(digest.encode("utf-8"),
   signature, signer.public_key_b64)` MUST return true. This binds every
   signed field — including both step roots, `verdicts`, and `charter_hash` — to
   the signer's key.
6. **Optional: charter linkage check** — if `charter_hash` is non-null and
   the verifier holds the referenced charter document, recompute its
   `compute_charter_hash` and compare; mismatch means the envelope does not
   commit to that charter. This step is strictly optional: the envelope is
   fully verifiable without it, and **DID resolution is never required** for
   verification.

Any failure at steps 1–5 means the attestation is invalid; there are no
partial-trust states in v1.

## Worked example

A minimal two-step run (`search_docs` then `summarize`), signed with the
example-only Ed25519 seed `00 01 02 … 1f` (base64
`AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8=`). All hashes, both step
commitment roots, and the signature are **real values** computed exactly per
this RFC, so the example doubles as an implementation test vector: recomputing
from the envelope below yields chain head `h_0 =
181c12df1f221316f3353e5a153d2cd2a79672b30f3830f83ad325dbaade707d` for step 0
(the intermediate `args_hash`/`output_hash` values hash the example args
`{"query":"refund policy"}` / output `{"documents":["policy-v3"],"count":1}`
and `{"text":"policy-v3 contents","max_words":50}` / output
`{"summary":"Refunds within 30 days."}` respectively), the signed digest is
`7f7ca86fdbc4ffd3cae7acb03f7f8f93b580d0a76c8a601a703f6ce56c79d8ef`, and the
signature verifies against the embedded public key. Step 1 carries
`cdv_bp: 910`, the basis-point rendering of a `0.91` CDV score; step 0 has
no score and omits the field. Every value below is emitted by
`docs/spec/test-vectors/generate_run_attestation_golden.py`, which also
writes the shared test vectors, so the example is reproducible rather than
transcribed. Because the example satisfies the charset rule, an RFC 8785
rendering of each step and of the unsigned envelope yields these same bytes.

Note that `h_0` is the raw digest of `s_0` and so is unaffected by the linking
rule; it is `steps_root` — the *chained* value — that depends on it. The two
roots over these same two steps are distinct values, as "Step commitments"
requires for `n ≥ 1`.

```json
{
  "format": "orcha.run-attestation/v1",
  "run_id": "run-9c2e-example",
  "agent_dids": [
    "did:orcha:agent:example-support-bot"
  ],
  "charter_hash": null,
  "policy_version": "example-policy/1.0",
  "steps": [
    {
      "seq": 0,
      "call_id": "call-7f3a",
      "tool": "search_docs",
      "args_hash": "cfb7b9e24993e2079be817f26458fbfe854c97ecca332a83bf03bc33912064a0",
      "output_hash": "b53a450afe33a989f27439fb2832a7258f4dd4ed2b883c82d34571d9c8b713e6",
      "success": true,
      "latency_ms": 132
    },
    {
      "seq": 1,
      "call_id": "call-7f3b",
      "tool": "summarize",
      "args_hash": "49d58a9a2fde08cd152b791aaa7ab455d90019dffcc646c7fef87c9e25762134",
      "output_hash": "32cd234e055db6061fb557f3d4dafdbccad80c904b9342b52b53f8870d0a80bd",
      "success": true,
      "latency_ms": 481,
      "cdv_bp": 910
    }
  ],
  "steps_root": "8de66de79f1eeab8c984588fd82aaab604a018ead0f04af43102ea2baab2557e",
  "steps_merkle_root": "96267180943447e517b2698c400a785f69bb67bff99abd1c3f9738512f9e4362",
  "verdicts": [
    {
      "check": "authorized_scope",
      "result": "pass"
    }
  ],
  "started_at": "2026-08-06T00:58:21Z",
  "finished_at": "2026-08-06T00:58:24Z",
  "signer": {
    "did": "did:orcha:system:validator",
    "public_key_b64": "A6EHv/POEL4dcN0Y50vAmWfk1jCbpQ1fHdyGZBJVMbg="
  },
  "signature": "Xwbefu8AXyI9wXgHapNu/GXGhinwF9jWlfnoi8qsevpzi3T0E+pHmymBEqO4/iJ9hMBKXEb7NTHQyEPirxDgDw=="
}
```

## Compatibility / migration

- **No emerge.yaml schema change.** This RFC defines a new artifact, not a
  manifest field; `schema_version` and `docs/spec/emerge-yaml.schema.json`
  are untouched, and no manifest migration is needed.
- **Purely additive to the runtime.** Producing an attestation is a
  post-execution concern wired through the `ExecutionObserver` seam; runs
  without attestation are unaffected.
- **Crypto reuse, not invention.** Canonicalisation, key handling
  (`ATTESTATION_PRIVATE_KEY_B64`, base64 32-byte seed, ephemeral dev
  fallback), and verification reuse the existing envelope/charter/validator
  code paths verbatim.
- The `format` version suffix is the only evolution mechanism: breaking
  envelope changes require `v2` and a new RFC; additive optional fields may
  extend `v1` (consumers must reject unknown fields when *validating* but
  treat absence per each field's own rule). The unknown-field rejection in
  the verification algorithm applies to verifiers of a given version: a `v1`
  verifier will reject an envelope carrying a later extension field, so
  additive extensions only become usable once verifiers have been updated to
  recognise them.
- **Envelope-level vs profile-level constraints.** Everything the schema
  block states — the field sets, the types, the charset rule, the hex,
  base64 and timestamp shapes, the general DID pattern, the `cdv_bp` range —
  is envelope-level and frozen by `format`. The DID *method* restriction
  (`did:orcha:agent:*` / `did:orcha:system:*`) is profile-level: the platform
  enforces it at production and at settlement, and it can change without a
  `v2`.

## Consumption points (informational)

- **SuperAgent** emits the envelope at run completion through the
  `ExecutionObserver` seam (`middleware/pipeline.py`), never inline in
  `execute_agent_calls.py` or `runner.py`, per the AGENTS.md protocol-dispatch
  contract.
- **Validator** (`services/validator/src/validator/signer.py`) holds the
  signing key and can countersign or persist envelopes alongside case
  attestations; the signer DID uses the `did:orcha:system:*` namespace.
- **Registry** may persist envelopes (nullable JSON column) and serve them to
  supervisory/audit endpoints.
- **Verifiers** (supervisors, auditors, counterparties) run the offline
  algorithm above with zero platform dependencies.

## Alignment (informational)

This envelope is self-contained and imports no external schemas, but it is
deliberately aligned with two external efforts:

- **TRACE v0.1** (`agentrust-io/trace-spec`) — an EAT (RFC 9711) profile for
  portable agent runtime evidence. Rough mapping: `format` ↔ EAT profile
  claim, `agent_dids` / `signer.did` ↔ agent identity claims, `steps` +
  `steps_root` ↔ the hash-chained tool transcript, `charter_hash` ↔ policy
  binding, `started_at`/`finished_at` ↔ issuance/expiry timestamps. TRACE
  additionally requires hardware (TEE) measurement claims; this envelope has
  no hardware-rooted claims in v1, and TRACE's CWT/JWT carrier is not used.
- **A2A Identity Trust Framework** (in flight) — the signer-DID-plus-
  offline-verification model here is compatible with its direction; concrete
  claim mapping is deferred until that framework stabilises.

Where these standards stabilise, a future RFC may define a mapping or
carrier; v1 deliberately does not depend on either.

## Alternatives considered

- **Embed raw args/outputs in the envelope** — rejected: violates
  privacy-by-design (tool payloads routinely contain PII and client data) and
  bloats the artifact; hashes give tamper evidence while allowing selective
  later disclosure.
- **A Merkle tree *instead of* the linear chain** — rejected: the two
  commitments answer different questions. The chain binds order directly and
  is the cheaper check for the common case of verifying a complete
  transcript; the tree binds membership at a position. `v1` carries both
  rather than choosing between them.
- **The linear chain alone, deferring a Merkle root to `v2`** — rejected, and
  it was this RFC's own earlier position. It does not survive its own escape
  hatch: this envelope is `additionalProperties: false` and verifiers reject
  unknown fields, so a root added after publication is rejected by every
  deployed `v1` verifier. "Additive in `v1`" and "additive after `v1`" are
  different things and only the first exists. Deferring the field does not
  defer the cost; it converts a field into a format version.
- **A Merkle root present but optional** — rejected: an optional commitment
  cannot be relied on, so nothing may be built on it, so the envelopes that
  need it are precisely the ones that omit it. Required, or absent.
- **Linking the chain on the 64-byte ASCII hex rendering of the previous
  digest** — rejected, and it was this RFC's own earlier construction. It
  needs a bespoke definition to state, obliges a re-implementer to get an
  unusual concatenation exactly right, and hashes one extra 32-byte word per
  step. Hexadecimal belongs where it is read — at the JSON boundary — and the
  linking bytes are raw.
- **A floating-point score field in `[0.0, 1.0]` with a normative
  number-rendering rule** —
  rejected, and it was this RFC's own earlier field. The one non-integer
  number in the envelope obliged every re-implementation to reproduce
  CPython's shortest-round-trip float formatting byte-for-byte — a
  conformance burden carried by every verifier for one optional field — and
  it was the sole obstacle to RFC 8785 parity. Basis points carry the same
  information as an integer. The field is renamed and kept, not dropped: the
  envelope is `additionalProperties: false`, so removing a field now and
  re-adding it later would be a breaking change, not an additive one.
- **Sign the canonical envelope bytes directly** (the `sign_manifest`
  convention) — rejected: the charter/case-attestation convention (sign the
  hex digest) keeps one signing and verification path across all signed
  platform artifacts.
- **Adopt TRACE/EAT (CWT/JWT) as the carrier** — deferred: pulls in a
  COSE/JWT stack and TEE-claim semantics the runtime does not have; the
  alignment notes above keep a mapping path open.

## Non-goals

- **No chain anchoring** — no testnet/blockchain or transparency-log
  anchoring of `steps_root` in v1; the validator's existing anchoring seam
  stays a separate, optional layer.
- **No token economics** — no tokenomics, staking, or launch mechanics
  anywhere in this specification.
- **No raw content embedding** — the envelope carries hashes only, by design.
- **No revocation** — key rotation, revocation lists, and expiry semantics
  are out of scope for v1.
