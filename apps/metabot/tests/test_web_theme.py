"""The UI's look (U0): the site's tokens, AA contrast computed from them, the self-hosted
fonts and their licences, the static cache, the policy, the theme switch, and the shell."""

from __future__ import annotations

import hashlib
import re
import tomllib
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from metabot import web_pages, web_theme
from metabot.web_app import make_app
from metabot.web_guard import COOKIE, Guard
from metabot.web_theme import FONTS, LICENSES, STATIC_CACHE, TEXT_PAIRS, THEMES, TOKENS, UI_PAIRS

PORT = 47011
TOKEN = "u" * 43
BASE = f"http://127.0.0.1:{PORT}"
PKG = Path(__file__).resolve().parents[1]
FONT_DIR = PKG / "src" / "metabot" / "static" / "fonts"
SOURCES = {  # sha256 of each file in the landing repo it was copied from (11-04)
    "08949f728dc52d528e69b1667d15c89a5686a4ee9a296ff90983985f99c380f7",  # Plex Mono latin 400
    "01d285447409c8a588692162439a038b8cbd7871309ee20267b0d2d91c6e8e22",  # Plex Mono latin 500
    "0d1f0b8d0722224e32e9f28261bdc86c79115be73444ae5eceb73976a1bcdf83",  # Plex Mono latin 600
    "5dfb755f0b23f4f5a369063564ca08fb551c9357bc51e0adcd5fc4eb293129e4",  # GeistPixel-Line
}


# ── contrast, computed from the tokens ──────────────────────────────────────


def _rgba(value: str) -> tuple[tuple[float, float, float], float]:
    """A token's colour as 0-255 channels and an alpha."""
    if value.startswith("#") and len(value) == 7:
        return (int(value[1:3], 16), int(value[3:5], 16), int(value[5:7], 16)), 1.0
    found = re.fullmatch(r"rgba\((\d+), (\d+), (\d+), (0?\.\d+|1)\)", value)
    assert found, f"{value!r} is neither #rrggbb nor rgba(r, g, b, a)"
    r, g, b, a = found.groups()
    return (float(r), float(g), float(b)), float(a)


def _over(tokens: dict[str, str], stack: tuple[str, ...]) -> tuple[float, float, float]:
    """The colour a stack of tokens paints, bottom first: an rgba one is blended over the rest."""
    rgb, alpha = _rgba(tokens[stack[0]])
    assert alpha == 1.0, f"{stack[0]} is the bottom of a stack, so it must be opaque"
    for name in stack[1:]:
        top, alpha = _rgba(tokens[name])
        rgb = tuple(alpha * t + (1 - alpha) * b for t, b in zip(top, rgb, strict=True))
    return rgb


def _luminance(rgb: tuple[float, float, float]) -> float:
    def channel(value: float) -> float:
        c = value / 255
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (channel(v) for v in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def ratio(fg: tuple[float, float, float], bg: tuple[float, float, float]) -> float:
    high, low = sorted((_luminance(fg), _luminance(bg)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def _low(theme: str, pairs, least: float) -> list[str]:
    tokens = TOKENS[theme]
    return [
        f"{fg} on {'+'.join(stack)}: {ratio(_over(tokens, (fg,)), _over(tokens, stack)):.2f}"
        for fg, stack in pairs
        if ratio(_over(tokens, (fg,)), _over(tokens, stack)) < least
    ]


def test_the_contrast_math_gives_the_known_ratios():
    white, black = (255.0, 255.0, 255.0), (0.0, 0.0, 0.0)
    assert ratio(black, white) == pytest.approx(21.0)
    assert ratio((119.0, 119.0, 119.0), white) == pytest.approx(4.48, abs=0.01)
    # the site's red on the site's paper: the reason the state tokens are our own
    assert ratio(_rgba("#dc2626")[0], _rgba("#f5f5f4")[0]) == pytest.approx(4.43, abs=0.01)


@pytest.mark.parametrize("theme", THEMES)
def test_every_text_token_reads_at_aa_where_it_sits(theme):
    assert _low(theme, TEXT_PAIRS, 4.5) == []


@pytest.mark.parametrize("theme", THEMES)
def test_focus_rings_and_field_edges_stand_out_at_three_to_one(theme):
    assert _low(theme, UI_PAIRS, 3.0) == []


def test_a_failing_token_is_caught(monkeypatch):
    light = {**TOKENS["light"], "--bad": "#dc2626"}  # the site's red
    monkeypatch.setitem(TOKENS, "light", light)
    assert any(line.startswith("--bad on --bg") for line in _low("light", TEXT_PAIRS, 4.5))


def test_the_state_tokens_are_our_own_and_each_is_read_on_its_wash():
    pairs = set(TEXT_PAIRS)
    for theme in THEMES:
        tokens = TOKENS[theme]
        assert tokens["--bad"] != "#dc2626" and "--warn" in tokens  # the site has no amber
    for state in ("--ok", "--warn", "--bad"):
        for surface in ("--bg", "--surface"):
            assert (state, (surface,)) in pairs
            assert (state, (surface, f"{state}-wash")) in pairs
    for text in ("--fg", "--muted", "--accent-text"):
        assert {(text, ("--bg",)), (text, ("--surface",))} <= pairs
    assert ("--on-accent", ("--accent-btn",)) in pairs
    assert {fg for fg, _ in UI_PAIRS} == {"--focus", "--field"}


def test_every_token_is_used_by_the_stylesheet():
    css = web_theme.stylesheet()
    unused = [name for name in TOKENS["light"] if f"var({name})" not in css]
    assert unused == []  # nothing kept that the pages do not draw


def test_both_themes_name_the_same_tokens():
    assert set(TOKENS["light"]) == set(TOKENS["dark"])
    css = web_theme.stylesheet()
    for theme in THEMES:
        for name, value in TOKENS[theme].items():
            assert f"{name}: {value};" in css, (theme, name)
    assert ':root[data-theme="dark"]' in css


# ── fonts and their licences ────────────────────────────────────────────────


def test_each_font_is_named_by_its_hash_and_is_its_source():
    assert {f.sha256 for f in FONTS} == SOURCES
    for font in FONTS:
        data = (FONT_DIR / f"{font.sha256}.woff2").read_bytes()
        assert hashlib.sha256(data).hexdigest() == font.sha256
        assert data[:4] == b"wOF2"
    assert sorted((f.family, f.weight) for f in FONTS) == [
        ("Geist Pixel", 400),
        ("IBM Plex Mono", 400),
        ("IBM Plex Mono", 500),
        ("IBM Plex Mono", 600),
    ]
    shipped = {p.name for p in FONT_DIR.iterdir()}
    assert shipped == {f"{f.sha256}.woff2" for f in FONTS} | set(LICENSES)


def test_both_licences_ship_with_the_fonts_and_the_config_declares_them():
    for name, holder in (("OFL-IBM-Plex-Mono.txt", "IBM Corp"), ("OFL-Geist-Pixel.txt", "Vercel")):
        text = (FONT_DIR / name).read_text(encoding="utf-8")
        assert "SIL OPEN FONT LICENSE Version 1.1" in text and holder in text, name
    config = tomllib.loads((PKG / "pyproject.toml").read_text(encoding="utf-8"))
    declared = config["project"]["license-files"]
    assert "LICENSE" in declared
    for name in LICENSES:
        assert f"src/metabot/static/fonts/{name}" in declared, name
    # the wheel takes every file under the package, these too: nothing narrows it
    wheel = config["tool"]["hatch"]["build"]["targets"]["wheel"]
    assert wheel["packages"] == ["src/metabot"]
    assert not {"exclude", "only-include", "sources"} & set(wheel)


def test_the_stylesheet_loads_each_font_from_this_server():
    css = web_theme.stylesheet()
    for font in FONTS:
        face = f"url(/static/fonts/{font.sha256}.woff2)"
        assert face in css, font
    assert css.count("@font-face") == len(FONTS) and css.count("font-display: swap") == len(FONTS)
    assert "http" not in css  # nothing from anywhere else


# ── served: the fonts cached, every other response not ──────────────────────


def _guard() -> Guard:
    return Guard(TOKEN, PORT)


def _client(tmp_path: Path, guard: Guard | None = None, signed_in: bool = True) -> TestClient:
    client = TestClient(
        make_app(guard or _guard(), start_dir=tmp_path), base_url=BASE, follow_redirects=False
    )
    if signed_in:
        client.cookies.set(COOKIE, TOKEN)
    return client


def test_a_font_is_cached_for_good_and_every_other_response_is_not(tmp_path):
    client = _client(tmp_path)
    for font in FONTS:
        response = client.get(f"/static/fonts/{font.sha256}.woff2")
        assert response.status_code == 200
        assert response.headers["content-type"] == "font/woff2"
        assert response.headers["cache-control"] == STATIC_CACHE
        assert response.content == (FONT_DIR / f"{font.sha256}.woff2").read_bytes()
    assert STATIC_CACHE == "public, max-age=31536000, immutable"
    sha = FONTS[0].sha256
    for path in (
        "/",
        "/runs",
        "/static/fonts/nothing.woff2",
        "/static/fonts/OFL-Geist-Pixel.txt",  # a licence ships, but is not served
        f"/static/fonts/{sha}.woff",
        f"/static/fonts/{sha.upper()}.woff2",
        "/static/fonts/..%2F..%2Fweb_app.py",
        "/static/",
    ):
        response = client.get(path)
        assert response.headers["cache-control"] == "no-store", path
        assert response.status_code in (200, 404), (path, response.status_code)
    refused = _client(tmp_path, signed_in=False).get(f"/static/fonts/{sha}.woff2")
    assert refused.status_code == 401 and refused.headers["cache-control"] == "no-store"


def _policy(response) -> dict[str, list[str]]:
    parts = [part.split() for part in response.headers["content-security-policy"].split(";")]
    return {words[0]: words[1:] for words in parts if words}


def test_the_policy_takes_fonts_and_scripts_from_this_server_alone(tmp_path):
    response = _client(tmp_path).get("/")
    policy = _policy(response)
    assert policy["default-src"] == ["'none'"]
    assert policy["font-src"] == ["'self'"]
    assert policy["script-src"] == ["'self'"]
    assert policy["connect-src"] == ["'self'"]
    [nonce] = policy["style-src"]
    assert re.fullmatch(r"'nonce-[A-Za-z0-9_-]+'", nonce)  # never 'unsafe-inline'
    assert "img-src" not in policy  # the mark is drawn in the page, not fetched
    assert f'<style nonce="{nonce[7:-1]}">' in response.text


# ── the theme: a form, a cookie, and the server draws it ────────────────────


def _switch(client: TestClient, guard: Guard, **fields):
    data = {"csrf": guard.csrf, "theme": "dark", "back": "/runs", **fields}
    return client.post("/theme", data=data, headers={"origin": guard.origin})


def test_the_theme_is_switched_by_a_form_and_kept_in_a_cookie(tmp_path):
    guard = _guard()
    client = _client(tmp_path, guard)
    assert '<html lang="en" data-theme="light">' in client.get("/runs").text
    response = _switch(client, guard)
    assert response.status_code == 303 and response.headers["location"] == "/runs"
    cookie = response.headers["set-cookie"]
    assert cookie.startswith(f"{web_theme.THEME_COOKIE}=dark;")
    parts = {p.strip().lower() for p in cookie.split(";")[1:]}
    assert {"httponly", "path=/", "samesite=strict", "max-age=31536000"} <= parts
    assert '<html lang="en" data-theme="dark">' in client.get("/runs").text
    assert _switch(client, guard, theme="light").status_code == 303
    assert '<html lang="en" data-theme="light">' in client.get("/runs").text


def test_a_theme_cookie_that_names_no_theme_draws_the_light_one(tmp_path):
    client = _client(tmp_path)
    for value in ("purple", "", "DARK", "dark,light"):
        client.cookies.set(web_theme.THEME_COOKIE, value)
        assert 'data-theme="light"' in client.get("/runs").text, value


def test_a_theme_switch_needs_the_form_token_and_the_origin(tmp_path):
    guard = _guard()
    client = _client(tmp_path, guard)
    assert _switch(client, guard, csrf="wrong").status_code == 403
    data = {"csrf": guard.csrf, "theme": "dark", "back": "/"}
    assert (
        client.post("/theme", data=data, headers={"origin": "http://evil.test"}).status_code == 403
    )
    assert client.post("/theme", data=data).status_code == 403
    assert "set-cookie" not in _switch(client, guard, csrf="").headers


def test_an_unknown_theme_is_refused(tmp_path):
    guard = _guard()
    response = _switch(_client(tmp_path, guard), guard, theme="purple")
    assert response.status_code == 400 and "set-cookie" not in response.headers


@pytest.mark.parametrize(
    ("back", "location"),
    [
        ("//evil.test/x", "/evil.test/x"),
        ("/\\evil.test", "/%5Cevil.test"),
        ("https://evil.test/", "/"),
        ("runs", "/"),
        ("", "/"),
        ("/office?folder=/tmp", "/office?folder=/tmp"),
        ("/a\r\nSet-Cookie: x=1", "/a%0D%0ASet-Cookie%3A%20x=1"),
    ],
)
def test_the_theme_switch_goes_back_to_this_address_alone(tmp_path, back, location):
    guard = _guard()
    response = _switch(_client(tmp_path, guard), guard, back=back)
    assert response.status_code == 303 and response.headers["location"] == location
    assert response.headers.get_list("set-cookie")[0].startswith("metabot_theme=dark;")
    assert len(response.headers.get_list("set-cookie")) == 1


# ── the shell every page shares ─────────────────────────────────────────────

PAGES = (
    "/", "/plan", "/runs", "/chat", "/office", "/connections", "/routines", "/runs/no-such-run",
)  # fmt: skip


def test_every_page_has_the_shell_and_no_script_or_inline_style(tmp_path):
    guard = _guard()
    client = _client(tmp_path, guard)
    for path in PAGES:
        text = client.get(path).text
        assert '<meta name="viewport" content="width=device-width, initial-scale=1">' in text
        assert '<header class="top">' in text and '<svg class="mark"' in text, path
        assert "<script" not in text and " style=" not in text, path
        switch = text[text.index('<form class="theme"') :].split("</form>", 1)[0]
        assert 'method="post" action="/theme"' in switch
        assert f'name="csrf" value="{guard.csrf}"' in switch
        assert f'name="back" value="{path}"' in switch, path
        assert '<button name="theme" value="dark">Dark theme</button>' in switch


def test_the_nav_marks_the_page_you_are_on(tmp_path):
    text = _client(tmp_path).get("/runs").text
    assert '<a href="/runs" aria-current="page">Runs</a>' in text
    assert '<a href="/" aria-current' not in text


def test_the_mark_is_drawn_with_classes_alone():
    svg = web_theme.mark()
    assert svg.startswith('<svg class="mark"') and 'aria-hidden="true"' in svg
    assert "style" not in svg and "fill=" not in svg
    for part in ("mark-rim", "mark-plate", "mark-eye"):
        assert f'class="{part}"' in svg
        assert f".{part} {{" in web_theme.stylesheet()


def test_a_narrow_screen_gets_one_column_and_buttons_44_px_high():
    css = web_theme.stylesheet()
    narrow = css[css.index("@media (max-width: 640px)") :]
    assert "min-height: 44px" in narrow and "flex-direction: column" in narrow


def test_a_page_drawn_outside_a_request_is_light_and_has_no_switch():
    page = web_pages.layout("Runs", "<p>x</p>", "n0nce")
    assert '<html lang="en" data-theme="light">' in page and 'action="/theme"' not in page


@pytest.mark.parametrize(
    ("state", "cls"),
    [
        ("valid", "ok"),
        ("valid, 4 members", "ok"),
        ("no receipt", "none"),
        ("INVALID", "bad"),
        ("JOURNAL MISMATCH", "bad"),
        ("cannot read", "bad"),
    ],
)
def test_a_state_colour_always_carries_its_word(state, cls):
    assert web_pages._state(state) == f'<span class="{cls}">{state}</span>'
    css = web_theme.stylesheet()
    assert f"span.{cls}" in css
