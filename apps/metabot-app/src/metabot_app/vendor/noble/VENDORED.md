# Vendored noble crypto (read before touching anything here)

`vendor/mbverify.js` needs SHA-256, SHA-512 and Ed25519 arithmetic on a page
that is served over plain http, where the browser gives no `crypto.subtle`.
These files are the smallest import closure of two noble entry points that
provide that, copied from disk. Nothing was downloaded and npm was not run.

| Package | Version | Licence | Copied from |
|---|---|---|---|
| `@noble/curves` | 1.9.7 | MIT (`curves/LICENSE`) | `~/orcha-escrow/program/node_modules/@noble/curves/esm/` |
| `@noble/hashes` | 1.8.0 | MIT (`hashes/LICENSE`) | `~/orcha-escrow/program/node_modules/@noble/hashes/esm/` |

Entry points and their transitive closure (12 files):

```
curves/ed25519.js            <- @noble/curves/esm/ed25519.js
curves/utils.js
curves/abstract/curve.js
curves/abstract/edwards.js
curves/abstract/hash-to-curve.js
curves/abstract/modular.js
curves/abstract/montgomery.js
hashes/sha2.js               <- @noble/hashes/esm/sha2.js
hashes/_md.js
hashes/_u64.js
hashes/utils.js
hashes/crypto.js
```

File names and directory layout are the upstream `esm/` layout with the
`esm/` segment dropped, so `curves/esm/abstract/edwards.js` became
`curves/abstract/edwards.js`.

## The one rewrite

Upstream files import each other through bare package specifiers that only a
bundler or Node's package resolution can follow. A browser `<script
type="module">` cannot, so every bare `@noble/hashes/...` specifier was
rewritten to the relative path of the vendored copy. Nothing else in any
file changed; `diff` against the source shows only these five lines:

| File | Was | Now |
|---|---|---|
| `curves/ed25519.js` | `'@noble/hashes/sha2.js'` | `'../hashes/sha2.js'` |
| `curves/ed25519.js` | `'@noble/hashes/utils.js'` | `'../hashes/utils.js'` |
| `curves/utils.js` (two lines) | `'@noble/hashes/utils.js'` | `'../hashes/utils.js'` |
| `hashes/utils.js` | `'@noble/hashes/crypto'` | `'./crypto.js'` |

The last one deserves a note. Upstream `@noble/hashes/crypto` is an export
map entry with two targets: `cryptoNode.js` (imports `node:crypto`) under the
`node` condition and `crypto.js` otherwise. The browser target was chosen.
`crypto.js` only reads `globalThis.crypto` once at import time, guarded by an
`in` check, and the value feeds `randomBytes`, which the verifier never
calls. Verification touches no WebCrypto; `tests/mbverify.test.mjs` proves it
by running the whole parity corpus in a Node child started with
`--no-experimental-global-webcrypto` and `globalThis.crypto` deleted.

## Re-vendoring

Repeat the copy from the same source path (or a newer pinned version), keep
the file names, apply only the specifier rewrite above, copy both LICENSE
files, update the table here, then run `node --test tests/mbverify.test.mjs`
(it checks that every specifier under `vendor/noble/` is relative and
resolves to a file in this folder).
