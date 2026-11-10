"""How ``metabot ui`` looks: the site's tokens, light and dark, its fonts, its mark, and the
components the pages share.

The tokens are the landing site's, so the app and the site read as one thing. Three are
our own: the state colours ``--ok``, ``--warn`` and ``--bad``, because the site's red does
not reach AA on its paper and the site has no amber, and ``--field``, an input's edge,
which the site's hairlines leave below 3:1. A state colour never stands alone: it always
sits on a word. ``TEXT_PAIRS`` and ``UI_PAIRS`` say what is read on what, and a test
computes every ratio from the values here.

The fonts are served from this server, named by their sha256, so a name never changes
its bytes and the browser may keep them for good. The theme is a cookie the server reads:
it draws ``data-theme`` on the page, so the switch needs no script.
"""

from __future__ import annotations

import functools
import hashlib
from contextvars import ContextVar
from dataclasses import dataclass
from importlib import resources
from urllib.parse import quote

from starlette.datastructures import Headers
from starlette.types import ASGIApp, Receive, Scope, Send

THEMES = ("light", "dark")
THEME_COOKIE = "metabot_theme"
THEME_KEEP_S = 31_536_000  # a year: the choice outlives each start of the UI
STATIC = "/static/"
STATIC_CACHE = "public, max-age=31536000, immutable"  # a file under /static/ is named by its hash

TOKENS: dict[str, dict[str, str]] = {
    "light": {
        "--bg": "#f5f5f4",
        "--surface": "#ffffff",
        "--fg": "#171717",
        "--muted": "#57534e",
        "--line": "rgba(23, 23, 23, 0.12)",
        "--line-strong": "rgba(23, 23, 23, 0.28)",
        "--field": "#8a857f",
        "--accent": "#ff5f1f",
        "--accent-text": "#c2410c",
        "--accent-btn": "#c2410c",
        "--accent-btn-hover": "#9a3412",
        "--on-accent": "#ffffff",
        "--tag-bg": "rgba(23, 23, 23, 0.05)",
        "--focus": "#c2410c",
        "--shadow": "0 1px 2px rgba(23, 23, 23, 0.06)",
        "--ok": "#166534",
        "--ok-wash": "rgba(22, 101, 52, 0.08)",
        "--warn": "#8a4b08",
        "--warn-wash": "rgba(138, 75, 8, 0.08)",
        "--bad": "#b42318",
        "--bad-wash": "rgba(180, 35, 24, 0.08)",
        "--mark-plate": "#141210",
        "--mark-rim": "#3c3b39",
    },
    "dark": {
        "--bg": "#0c0b0a",
        "--surface": "#151311",
        "--fg": "#f5f5f4",
        "--muted": "#a8a29e",
        "--line": "rgba(245, 245, 244, 0.12)",
        "--line-strong": "rgba(245, 245, 244, 0.28)",
        "--field": "#77716b",
        "--accent": "#ff5f1f",
        "--accent-text": "#ff8a4c",
        "--accent-btn": "#c2410c",
        "--accent-btn-hover": "#9a3412",
        "--on-accent": "#ffffff",
        "--tag-bg": "rgba(245, 245, 244, 0.06)",
        "--focus": "#ff5f1f",
        "--shadow": "0 0 #0000",
        "--ok": "#3ecf8e",
        "--ok-wash": "rgba(62, 207, 142, 0.1)",
        "--warn": "#f0b341",
        "--warn-wash": "rgba(240, 179, 65, 0.1)",
        "--bad": "#ff7b72",
        "--bad-wash": "rgba(255, 123, 114, 0.1)",
        "--mark-plate": "#050404",
        "--mark-rim": "#302f2f",
    },
}

# What is read on what: a text token, and the tokens painted under it, bottom first.
_SURFACES = (("--bg",), ("--surface",))
_TINTED = (("--bg", "--tag-bg"), ("--surface", "--tag-bg"))  # code panels and plain chips
TEXT_PAIRS: tuple[tuple[str, tuple[str, ...]], ...] = (
    *((text, under) for text in ("--fg", "--muted") for under in _SURFACES + _TINTED),
    *(("--accent-text", under) for under in _SURFACES),  # links and kickers: never on a tint
    *(
        (state, under)
        for state in ("--ok", "--warn", "--bad")
        for under in _SURFACES + tuple((base, f"{state}-wash") for (base,) in _SURFACES)
    ),
    ("--on-accent", ("--accent-btn",)),
    ("--on-accent", ("--accent-btn-hover",)),
)
UI_PAIRS: tuple[tuple[str, tuple[str, ...]], ...] = tuple(
    (edge, under) for edge in ("--focus", "--field") for under in _SURFACES
)


@dataclass(frozen=True)
class Font:
    family: str
    weight: int
    sha256: str  # its name under /static/fonts/, and the hash of the file it was copied from
    source: str  # where the landing repo got it
    latin: bool = False  # the latin subset alone: other characters fall back


FONTS: tuple[Font, ...] = (
    Font(
        "IBM Plex Mono",
        400,
        "08949f728dc52d528e69b1667d15c89a5686a4ee9a296ff90983985f99c380f7",
        "@fontsource/ibm-plex-mono 5.3.0 files/ibm-plex-mono-latin-400-normal.woff2",
        latin=True,
    ),
    Font(
        "IBM Plex Mono",
        500,
        "01d285447409c8a588692162439a038b8cbd7871309ee20267b0d2d91c6e8e22",
        "@fontsource/ibm-plex-mono 5.3.0 files/ibm-plex-mono-latin-500-normal.woff2",
        latin=True,
    ),
    Font(
        "IBM Plex Mono",
        600,
        "0d1f0b8d0722224e32e9f28261bdc86c79115be73444ae5eceb73976a1bcdf83",
        "@fontsource/ibm-plex-mono 5.3.0 files/ibm-plex-mono-latin-600-normal.woff2",
        latin=True,
    ),
    Font(
        "Geist Pixel",
        400,
        "5dfb755f0b23f4f5a369063564ca08fb551c9357bc51e0adcd5fc4eb293129e4",
        "geist 1.7.2 dist/fonts/geist-pixel/GeistPixel-Line.woff2",
    ),
)
LICENSES = ("OFL-IBM-Plex-Mono.txt", "OFL-Geist-Pixel.txt")  # beside the fonts, and in the wheel
LATIN = (
    "U+0000-00FF, U+0131, U+0152-0153, U+02BB-02BC, U+02C6, U+02DA, U+02DC, U+0304, U+0308, "
    "U+0329, U+2000-206F, U+20AC, U+2122, U+2191, U+2193, U+2212, U+2215, U+FEFF, U+FFFD"
)
_FONT_FILES = frozenset(f"{font.sha256}.woff2" for font in FONTS)


def font_bytes(name: str) -> bytes | None:
    """The font ``/static/fonts/<name>`` serves, or None for a name that is none of ours."""
    return _read_font(name) if name in _FONT_FILES else None


@functools.cache
def _read_font(name: str) -> bytes:
    return (resources.files("metabot") / "static" / "fonts" / name).read_bytes()


# ── the live page's script ──────────────────────────────────────────────────

LIVE_SCRIPT = "live.js"  # beside the fonts; served under /static/live/ by its hash


@functools.cache
def _live_script() -> tuple[str, bytes]:
    """The script's bytes and their sha256, which names it under ``/static/live/``."""
    found = (resources.files("metabot") / "static" / LIVE_SCRIPT).read_bytes()
    return hashlib.sha256(found).hexdigest(), found


def live_script_path() -> str:
    """Where a page loads the live script from: a name that changes with its bytes."""
    return f"{STATIC}live/{_live_script()[0]}.js"


def live_script_bytes(name: str) -> bytes | None:
    """The script ``/static/live/<name>`` serves, or None for a name that is not its hash."""
    sha, found = _live_script()
    return found if name == f"{sha}.js" else None


# ── the mark: MetaBot's face, as the site draws it ──────────────────────────

OCTAGON = "29,0 71,0 100,29 100,71 71,100 29,100 0,71 0,29"
_INNER = "translate(5 5) scale(0.9)"  # the plate inside its rim
_EYES = ((34.6, 50), (65.4, 50))  # the owl's eyes at 70% of the plate
_EYE = 'rx="7.7" ry="10.5"'


def mark() -> str:
    """The mark for a page: classes only, so the page's style block colours it."""
    eyes = "".join(f'<ellipse class="mark-eye" cx="{x}" cy="{y}" {_EYE}/>' for x, y in _EYES)
    return (
        '<svg class="mark" viewBox="0 0 100 100" aria-hidden="true" focusable="false">'
        f'<polygon class="mark-rim" points="{OCTAGON}"/>'
        f'<polygon class="mark-plate" points="{OCTAGON}" transform="{_INNER}"/>{eyes}</svg>'
    )


def icon() -> str:
    """The mark as a file, in the light theme's colours, for the desktop launcher."""
    light = TOKENS["light"]
    eyes = "".join(
        f'<ellipse cx="{x}" cy="{y}" {_EYE} fill="{light["--accent"]}"/>' for x, y in _EYES
    )
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
        f'<polygon points="{OCTAGON}" fill="{light["--mark-rim"]}"/>'
        f'<polygon points="{OCTAGON}" transform="{_INNER}" fill="{light["--mark-plate"]}"/>'
        f"{eyes}</svg>\n"
    )


# ── the stylesheet ──────────────────────────────────────────────────────────

_COMPONENTS = """
*, *::before, *::after { box-sizing: border-box; }
html, body { background: var(--bg); color: var(--fg); }
body { margin: 0; font: 15px/1.6 var(--mono); -webkit-font-smoothing: antialiased; }
a { color: var(--accent-text); text-underline-offset: 3px; }
a:hover { color: var(--fg); }
:focus-visible { outline: 2px solid var(--focus); outline-offset: 2px; }
::selection { background: var(--accent-btn); color: var(--on-accent); }

.top { position: sticky; top: 0; z-index: 10; background: var(--bg);
       border-bottom: 1px solid var(--line); }
.bar { max-width: 1120px; margin: 0 auto; padding: 10px 24px; display: flex;
       align-items: center; flex-wrap: wrap; gap: 8px 28px; }
.brand { display: inline-flex; align-items: center; gap: 10px; color: var(--fg);
         font-weight: 600; text-decoration: none; }
.mark { width: 26px; height: 26px; flex: none; }
.mark-rim { fill: var(--mark-rim); }
.mark-plate { fill: var(--mark-plate); }
.mark-eye { fill: var(--accent); }
nav { display: flex; flex-wrap: wrap; gap: 4px 20px; }
nav a { color: var(--muted); text-decoration: none; padding: 6px 0; }
nav a:hover { color: var(--fg); }
nav a[aria-current="page"] { color: var(--fg); box-shadow: inset 0 -2px 0 var(--accent); }
form.theme { margin: 0 0 0 auto; }
form.theme button { padding: 6px 14px; font-weight: 500; }

main { max-width: 1120px; margin: 0 auto; padding: 28px 24px 40px; }
main.narrow { max-width: 680px; }
h1 { font: 400 28px/1.15 var(--display); margin: 6px 0 20px; }
h2 { font-size: 12px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.12em;
     color: var(--muted); margin: 32px 0 10px; }
p { margin: 12px 0; }
.kicker { font-size: 11px; text-transform: uppercase; letter-spacing: 0.22em;
          color: var(--accent-text); margin: 0; }
.lead { font-size: 16px; }
.muted { color: var(--muted); }
.indent { padding-left: 22px; }
footer { max-width: 1120px; margin: 0 auto; padding: 0 24px 28px; font-size: 13px; }

.card, form.ask, .why { background: var(--surface); border: 1px solid var(--line);
        border-radius: 16px; box-shadow: var(--shadow); padding: 18px 22px; margin: 16px 0; }
.actions { display: flex; flex-wrap: wrap; align-items: center; gap: 12px; margin: 20px 0 4px; }

button, .button { display: inline-flex; align-items: center; justify-content: center; gap: 8px;
       font: 600 13px/1.2 var(--mono); padding: 10px 20px; border-radius: 9999px;
       border: 1px solid var(--line-strong); background: transparent; color: var(--fg);
       cursor: pointer; text-decoration: none; }
button:hover, .button:hover { border-color: var(--accent); color: var(--fg); }
.primary, a.primary { background: var(--accent-btn); border-color: var(--accent-btn);
       color: var(--on-accent); }
.primary:hover, a.primary:hover { background: var(--accent-btn-hover);
       border-color: var(--accent-btn-hover); color: var(--on-accent); }

input, textarea, select { font: inherit; color: var(--fg); background: var(--surface);
       border: 1px solid var(--field); border-radius: 12px; padding: 9px 12px; max-width: 100%; }
textarea { width: 100%; display: block; }
label { cursor: pointer; }
.choice { display: flex; gap: 12px; align-items: flex-start; padding: 12px 16px;
       border: 1px solid var(--line); border-radius: 12px; background: var(--surface);
       margin: 8px 0; }
.choice input { margin-top: 5px; }
fieldset { border: 0; margin: 20px 0 0; padding: 0; min-width: 0; }
legend { padding: 0; font-size: 12px; font-weight: 600; text-transform: uppercase;
         letter-spacing: 0.12em; color: var(--muted); }
ul { padding-left: 22px; }
li { margin: 6px 0; }

table { border-collapse: collapse; width: 100%; }
th, td { text-align: left; vertical-align: top; padding: 8px 14px 8px 0;
         border-bottom: 1px solid var(--line); }
th { font-size: 12px; font-weight: 500; color: var(--muted); }
table.facts th { width: 9em; }

code, pre { font: 13px/1.55 var(--mono); }
code { background: var(--tag-bg); border-radius: 6px; padding: 1px 6px; }
pre { white-space: pre-wrap; word-break: break-word; background: var(--tag-bg);
      border: 1px solid var(--line); border-radius: 12px; padding: 12px 14px; }
pre code { background: none; padding: 0; }
summary { cursor: pointer; color: var(--muted); }

.ok { color: var(--ok); }
.warn { color: var(--warn); }
.bad { color: var(--bad); }
span.ok, span.warn, span.bad, span.none { display: inline-block; font-weight: 600;
       font-size: 13px; line-height: 1.6; padding: 0 10px; border-radius: 9999px; }
span.ok { background: var(--ok-wash); }
span.warn { background: var(--warn-wash); }
span.bad { background: var(--bad-wash); }
span.none { background: var(--tag-bg); color: var(--muted); }

@media (max-width: 640px) {
  .bar { flex-direction: column; align-items: flex-start; padding: 10px 16px; }
  form.theme { margin: 0; }
  main { padding: 20px 16px 32px; }
  footer { padding: 0 16px 24px; }
  h1 { font-size: 24px; }
  .actions { flex-direction: column; align-items: stretch; }
  button, .button, input, select { min-height: 44px; }
  table:not(.facts) { display: block; overflow-x: auto; }
  table.facts, table.facts tbody, table.facts tr, table.facts th, table.facts td {
    display: block; width: auto; }
  table.facts th { border-bottom: 0; padding-bottom: 0; }
}
"""


def _tokens(theme: str) -> str:
    return "".join(f"  {name}: {value};\n" for name, value in TOKENS[theme].items())


def _face(font: Font) -> str:
    latin = f"  unicode-range: {LATIN};\n" if font.latin else ""
    return (
        f'@font-face {{\n  font-family: "{font.family}";\n  font-style: normal;\n'
        f"  font-weight: {font.weight};\n  font-display: swap;\n"
        f'  src: url(/static/fonts/{font.sha256}.woff2) format("woff2");\n{latin}}}\n'
    )


@functools.cache
def stylesheet() -> str:
    """The style block every page carries: fonts, both themes' tokens, the components."""
    fonts = "".join(map(_face, FONTS))
    root = (
        ":root {\n  color-scheme: light;\n"
        '  --mono: "IBM Plex Mono", ui-monospace, SFMono-Regular, Menlo, monospace;\n'
        '  --display: "Geist Pixel", "IBM Plex Mono", ui-monospace, monospace;\n'
        f"{_tokens('light')}}}\n"
        f':root[data-theme="dark"] {{\n  color-scheme: dark;\n{_tokens("dark")}}}\n'
    )
    return fonts + root + _COMPONENTS


# ── the theme each request is drawn in ──────────────────────────────────────


@dataclass(frozen=True)
class Page:
    """What a page's shell needs from its request: the theme, the form token, the address."""

    theme: str = "light"
    csrf: str = ""  # none outside a request: the shell then has no theme switch
    path: str = "/"


NO_REQUEST = Page()  # frozen: a page drawn outside a request shares it
PAGE: ContextVar[Page] = ContextVar("metabot_page", default=NO_REQUEST)


def theme_of(cookie: str | None) -> str:
    """The theme a request's cookie names; light when it names none of ours."""
    for part in (cookie or "").split(";"):
        name, _, value = part.strip().partition("=")
        if name == THEME_COOKIE and value in THEMES:
            return value
    return "light"


def back_to(value: object) -> str:
    """Where a form sends the person back: a path on this server, else the home page.

    Never ``//host``, which is another site; quoted, so no line break reaches a header.
    """
    if not isinstance(value, str) or not value.startswith("/") or len(value) > 2000:
        return "/"
    return quote("/" + value.lstrip("/"), safe="/?=&%-._~")


def themed(app: ASGIApp, csrf: str) -> ASGIApp:
    """``app``, with each request's theme, form token and address set for its pages."""

    async def asgi(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await app(scope, receive, send)
            return
        query = scope.get("query_string", b"").decode("latin-1")
        path = scope["path"] + (f"?{query}" if query else "")
        token = PAGE.set(Page(theme_of(Headers(scope=scope).get("cookie")), csrf, path))
        try:
            await app(scope, receive, send)
        finally:
            PAGE.reset(token)

    return asgi
