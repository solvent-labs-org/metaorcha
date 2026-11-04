"""The guard in front of every page of ``metabot ui``.

The UI listens on 127.0.0.1 only, and every request needs the token made
when it started. The address it prints carries the token once; the first
visit trades it for a cookie that is HttpOnly and SameSite=Strict, and goes
back to the same page without it. A request for any other host name is
refused, so a web page that points a name of its own at this machine cannot
reach the UI. A request that changes anything must also come from the UI's
own origin and carry the form token rendered into its pages, so another
site open in the same browser cannot send one.

Every response, refused or not, carries headers that let no script run, no
frame hold the page, and no address leak to another site. The token is held
in memory only and never logged.

A limit: browsers send a cookie for 127.0.0.1 to every port on it, so any
other server the browser visits on this machine is sent the token while the
UI runs. It stops working when the UI stops.
"""

from __future__ import annotations

import hmac
import secrets
from dataclasses import dataclass, field
from urllib.parse import parse_qs, quote

from starlette.datastructures import FormData, Headers
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

COOKIE = "metabot_ui"
READS = frozenset({"GET", "HEAD"})


@dataclass(frozen=True)
class Guard:
    """What one start of the UI accepts: its token, its port, and its form token."""

    token: str
    port: int
    csrf: str = field(default_factory=lambda: secrets.token_urlsafe(32))

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def host(self) -> str:
        return f"127.0.0.1:{self.port}"

    def is_token(self, value: str | None) -> bool:
        return value is not None and hmac.compare_digest(value.encode(), self.token.encode())


def page_headers(nonce: str) -> dict[str, str]:
    """The headers on every response: no script, no frame, no referrer, no cache."""
    return {
        "content-security-policy": (
            f"default-src 'none'; style-src 'nonce-{nonce}'; form-action 'self'; "
            "frame-ancestors 'none'; base-uri 'none'"
        ),
        "x-frame-options": "DENY",
        "x-content-type-options": "nosniff",
        "referrer-policy": "no-referrer",
        "cache-control": "no-store",
    }


def nonce_of(request: Request) -> str:
    return request.scope["metabot_nonce"]


async def checked_form(request: Request, guard: Guard) -> FormData:
    """The posted form, once its form token is this UI's; HTTPException 403 otherwise."""
    form = await request.form()
    sent = form.get("csrf")
    if not isinstance(sent, str) or not hmac.compare_digest(sent.encode(), guard.csrf.encode()):
        raise HTTPException(403, "this form did not come from this page; reload it")
    return form


def _cookie(headers: Headers) -> str | None:
    for part in headers.get("cookie", "").split(";"):
        name, _, value = part.strip().partition("=")
        if name == COOKIE:
            return value
    return None


def guarded(app: ASGIApp, guard: Guard) -> ASGIApp:
    """``app`` behind the guard."""

    async def asgi(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await app(scope, receive, send)
            return
        if scope["type"] != "http":  # no websockets here
            await send({"type": "websocket.close", "code": 1008})
            return
        nonce = secrets.token_urlsafe(16)
        scope["metabot_nonce"] = nonce
        extra = [(k.encode(), v.encode()) for k, v in page_headers(nonce).items()]

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                names = {k for k, _ in extra}
                kept = [(k, v) for k, v in message.get("headers", []) if k.lower() not in names]
                message = {**message, "headers": kept + extra}
            await send(message)

        headers = Headers(scope=scope)
        if headers.get("host") != guard.host:
            await _refuse(send_with_headers, 403, f"open {guard.origin}/ instead")
            return
        query = parse_qs(scope.get("query_string", b"").decode("latin-1"))
        if "token" in query:
            if not guard.is_token(query["token"][-1]):
                await _refuse(send_with_headers, 401, _ASK)
                return
            # never //host, which is another site; quoted, so no line break reaches a header
            back = quote("/" + scope["path"].lstrip("/"), safe="/")
            await send_with_headers(
                {
                    "type": "http.response.start",
                    "status": 303,
                    "headers": [
                        (b"location", back.encode("ascii")),
                        (
                            b"set-cookie",
                            f"{COOKIE}={guard.token}; HttpOnly; Path=/; SameSite=Strict".encode(),
                        ),
                        (b"content-length", b"0"),
                    ],
                }
            )
            await send_with_headers({"type": "http.response.body", "body": b""})
            return
        if not guard.is_token(_cookie(headers)):
            await _refuse(send_with_headers, 401, _ASK)
            return
        if scope["method"] not in READS and headers.get("origin") != guard.origin:
            await _refuse(send_with_headers, 403, "a change must come from this page")
            return
        await app(scope, receive, send_with_headers)

    return asgi


_ASK = "metabot ui: open the address metabot ui printed when it started; it carries the token"


async def _refuse(send: Send, status: int, text: str) -> None:
    body = (text + "\n").encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"text/plain; charset=utf-8"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})
