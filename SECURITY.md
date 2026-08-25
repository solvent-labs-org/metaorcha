# Security Policy

## Reporting a vulnerability

**Please do not report security vulnerabilities through public GitHub issues,
discussions, or pull requests.**

Instead, report them privately so we can fix the issue before it is disclosed:

- Use GitHub's [private vulnerability reporting](https://github.com/solvent-labs-org/orcha/security/advisories/new)
  ("Report a vulnerability" via the repository's Security Advisories page).

Please include:

- A description of the vulnerability and its impact.
- Steps to reproduce (a minimal proof-of-concept if possible).
- Affected component(s) and version/commit.

## What to expect

- We aim to acknowledge reports within **72 hours**.
- We will keep you informed as we investigate and work on a fix.
- We will credit you in the release notes unless you prefer to remain anonymous.

## Scope

This policy covers the open-source Orcha runtime in this repository: the
SuperAgent execution pipeline, Registry, Planning & Discovery, Gateway (mock
mode), the example agents, the `common/*` libraries, and the `orcha-sdk` CLI/SDK
(`emerge` is kept as a compatibility alias).

Hosted/production payment rails are not part of this repository and are out of
scope here.

## Good practice for operators

- The public runtime ships in `PAYMENT_MODE=mock`. Do not expose a real-money
  configuration without a security review.
- Never commit credentials. `.gitignore` excludes common credential patterns
  (`*_SERV_ACC*.json`, `*.pem`, `.env`), but treat every secret as
  vault-managed, not file-managed.
