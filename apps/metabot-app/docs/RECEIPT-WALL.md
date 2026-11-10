# The receipt wall: what may be posted, and what is removed first

The receipt wall is the public list of receipts people have chosen to share. A receipt is
proof of what an agent did; it is not a transcript. This page is the rule for what crosses
from a machine to the wall, so that sharing a receipt never shares the work behind it.

## What a receipt holds

A receipt (`orcha.run-attestation/v1`) holds hashes, verdicts and timing: the sha256 of
the prompt and the answer, the tool names and the hashes of their inputs and outputs, the
engine, the model, the endpoint name and base URL, the signer's public key and the
signature. It never holds the prompt, the answer or a file's contents. Journals do; a
journal is never posted.

## What is removed before posting

Posting means publishing. Before a receipt goes on the wall, these are replaced in the
*copy that is posted*, never in the signed original (a changed original stops verifying,
and the wall must still verify what it shows; the wall says which fields were redacted):

| Field | Rule |
|---|---|
| `endpoint.base_url` | Replaced by its scheme and host class (`https://hosted`, `http://local`). A private hostname or an IP is never posted. |
| Paths in `content` details, `cwd`, tool arguments | Replaced by their basename, or by `<path>` when the basename itself names a person, a company or a client. |
| `signer` | Kept: it is the public key, and the point of the wall. The poster chooses the display name beside it. |
| `agent_dids` | Kept when they are `did:orcha:*`; anything else is replaced by `<did>`. |
| Account, login and device names (`login`, `endpoint.name`, device rows) | Replaced by `account-1`, `account-2`, in order of appearance. |
| Timestamps | Kept to the minute; seconds are dropped. |
| Anything a human recognises as a name of a real person, a client or an employer | Removed. The poster is asked once, in plain words, before the post is made. |

The redacted copy is marked `redacted: [fields]` beside the receipt, and the wall shows
the original's sha256 so a reader who holds the original can match it.

## What is never posted

- A journal (prompts, answers, tool outputs).
- A receipt from a run on a client's code or data, under any redaction, unless the client
  has said so in writing. The engagement's own receipts stay beside the engagement.
- A receipt whose signer is not the poster (you may post what you signed, not what someone
  else signed, unless they post it themselves).
- A token, a key, a certificate fingerprint or a pairing code, in any field.

## How a receipt gets there

1. `metabot receipts export <run> --out <dir>` makes the bundle; `metabot receipts verify
   --chain <dir>` must say VALID.
2. The redaction above is applied to the copy; the original stays with its owner.
3. The copy is posted by its owner, with the wall's own verifier run on it in public.

This rule is kept with the claims page: nothing on the wall says more than the receipt
proves.
