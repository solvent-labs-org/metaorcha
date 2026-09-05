# Spec fixtures

`demo-charter.json` — MVP demo charter for the KYA settlement slice. The
settling-path binding is the sha256 of this document's canonical JSON
(`emerge_node.envelope.canonical_json_bytes`), pinned and asserted in
`services/validator/tests/test_charter_fixture.py` (single source of truth
for the expected hash). Hash binding only — no CA chain (AD-9). The fixture deliberately does NOT
use the real AAC charter format (`common/charter`): hash-of-fixture is the
MVP binding; the full charter document + CA chain is the post-MVP layer.
