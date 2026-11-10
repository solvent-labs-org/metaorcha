# Pairing a phone

MetaBot for Android can drive a computer that runs the app server. Pairing is
one code, typed or scanned, and it happens once per phone.

## What happens

1. The computer keeps a self-signed certificate under its state folder (`tls/`),
   made once with `openssl`. Its SHA-256 fingerprint is the computer's identity.
2. **Settings → Phone** (or `metabot-app pair` in a terminal) shows a QR code, the
   address and port to type instead, an 8-character code that lasts ten minutes,
   and **four words** made from the fingerprint.
3. The phone reads the certificate from the address, shows its own four words and
   asks "Does your computer show these words?". Say **Yes** only if they match.
4. The phone sends the code over that pinned connection and gets a token of its
   own. The desktop token never leaves the computer. Five wrong codes kill the code.
5. From then on the phone talks to the TLS listener (port `phone_port`, 8461 by
   default) and nothing else. Removing the phone in Settings → Phone (or
   `metabot-app devices --remove <id>`) closes its open connections at once and
   refuses its next request.

The QR text is `metabot-pair:1?a=<addresses>&p=<port>&c=<code>&f=<fingerprint>`.
A change to that shape is a protocol change and needs an entry in the changelog.

## Addresses

The code advertises the computer's own IPv4 addresses (the default route first,
LAN before mesh-VPN ranges). Set `phone_addresses` in the config to advertise
something else, for example a hostname that only your network resolves. With a
USB cable and `adb reverse tcp:8461 tcp:8461`, the address to type on the phone
is `127.0.0.1:8461`.

## Config keys

| key | default | meaning |
|---|---|---|
| `phone_listener` | `true` | run the TLS listener; `false` = loopback only |
| `phone_port` | `8461` | the listener's port on every `phone_bind` address |
| `phone_bind` | `["0.0.0.0"]` | addresses the listener binds |
| `phone_addresses` | `[]` | addresses a code advertises; `[]` = the computer's own |

## Without openssl

The listener stays off and Settings → Phone says so. Install `openssl` (it ships
with every Linux distribution and with macOS) and restart the app.
