"""Origins: where a browser connection may act, read exactly as a browser reads them."""

from __future__ import annotations

import pytest
from mcp_types import Tool, ToolAnnotations

from metabot.connections import Connection
from metabot.origins import DEFAULT_ORIGINS, origin_allowed, origin_of, origin_pattern
from metabot.scope_classes import ScopeClass
from metabot.switchboard import class_label, classify

READ, WRITE, DESTRUCTIVE = ScopeClass.READ, ScopeClass.WRITE, ScopeClass.DESTRUCTIVE
DEV = "http://localhost:3000"


def _browser(**kw) -> Connection:
    return Connection(
        id="did:orcha:agent:conn-000000000001",
        name="web",
        transport="stdio",
        command=["browser"],
        origins=list(kw.pop("origins", DEFAULT_ORIGINS)),
        **kw,
    )


def _tool(name: str, **hints) -> Tool:
    annotations = ToolAnnotations(**hints) if hints else None
    return Tool(name=name, input_schema={"type": "object"}, annotations=annotations)


def _cls(name: str, args: dict | None = None, last: str | None = None, **kw) -> ScopeClass:
    return classify(_browser(**kw), _tool(name), args or {}, last)


# ── reading an origin ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("url", "origin"),
    [
        ("HTTP://LocalHost:3000/a/b?c=d#e", "http://localhost:3000"),
        ("http://localhost/", "http://localhost:80"),
        ("https://Example.COM", "https://example.com:443"),
        ("http://127.0.0.1:5173", "http://127.0.0.1:5173"),
        ("http://[::1]:8080/x", "http://[::1]:8080"),
        ("http://[0:0:0:0:0:0:0:1]:8080/", "http://[::1]:8080"),
        ("http://localhost:03000/", "http://localhost:3000"),
        ("http://my_app.test:3000/", "http://my_app.test:3000"),
    ],
)
def test_an_origin_is_scheme_host_and_port(url, origin):
    assert origin_of(url) == origin


def test_userinfo_trick_is_the_real_host():
    assert origin_of("http://localhost@evil.test/") == "http://evil.test:80"
    assert origin_of("http://localhost:3000@evil.test:8080/x") == "http://evil.test:8080"
    assert origin_of("http://a@localhost:3000@evil.test/") == "http://evil.test:80"


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "data:text/html,<script>alert(1)</script>",
        "javascript:alert(1)",
        "JavaScript:alert(1)",
        "about:blank",
        "blob:http://localhost:3000/1",
        "view-source:http://localhost:3000/",
        "ftp://localhost/",
        "ws://localhost:3000/",
        "",
        "/relative/path",
        "//evil.test/x",
        "localhost:3000",
        "http:localhost:3000",
        "http:/localhost:3000",
        "http:///evil.test",
    ],
)
def test_anything_but_an_absolute_http_url_has_no_origin(url):
    assert origin_of(url) is None


@pytest.mark.parametrize(
    "url",
    [
        "http://evil.test\\@localhost:3000/",  # a browser reads the backslash as a slash
        "http://localhost:3000\\@evil.test/",
        "http://evil.test\t@localhost/",
        "http://evil.test\n@localhost/",
        " http://localhost:3000/",
        "http://local host/",
        "http://localhost\x00.evil.test/",
        "http://local%68ost:3000/",  # a browser decodes the host first
        "http://ｌｏｃａｌｈｏｓｔ:3000/",  # fullwidth: a browser maps it, so it is refused
        "http://bücher.example/",
        "http://localhost:99999/",
        "http://localhost:x/",
        "http://localhost:-1/",
    ],
)
def test_what_a_browser_could_read_differently_has_no_origin(url):
    assert origin_of(url) is None


@pytest.mark.parametrize("value", [None, 3, ["http://localhost:3000"], {"url": DEV}, b"http://x"])
def test_a_url_that_is_not_text_has_no_origin(value):
    assert origin_of(value) is None


# ── the person's patterns ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "pattern"),
    [
        ("http://localhost:*", "http://localhost:*"),
        ("http://[::1]:*", "http://[::1]:*"),
        ("https://Staging.Example.com", "https://staging.example.com:443"),
        ("https://staging.example.com/", "https://staging.example.com:443"),
        ("http://127.0.0.1:8000", "http://127.0.0.1:8000"),
        ("  https://xn--bcher-kva.example  ", "https://xn--bcher-kva.example:443"),
    ],
)
def test_a_pattern_is_made_exact(text, pattern):
    assert origin_pattern(text) == pattern


@pytest.mark.parametrize(
    "text",
    [
        "localhost",
        "localhost:3000",
        "*.example.com",
        "https://*.example.com",
        "http://localhost:3000/admin",
        "http://localhost:3000?x=1",
        "http://localhost:3000#top",
        "http://user@localhost:3000",
        "ftp://localhost",
        "file:///tmp",
        "http://localhost:99999",
        "http://localhost:3*",
        "http://evil.test\\@localhost",
        "http://local\thost:3000",  # Python drops the tab
        "http://localhost:30\n00",
        "",
    ],
)
def test_what_is_not_an_origin_is_refused(text):
    with pytest.raises(ValueError, match="not an origin"):
        origin_pattern(text)


def test_a_host_that_is_not_ascii_is_asked_for_in_its_xn_form():
    with pytest.raises(ValueError, match="xn--"):
        origin_pattern("https://bücher.example")


def test_default_origins_are_this_machine_on_any_port():
    assert [origin_pattern(p) for p in DEFAULT_ORIGINS] == list(DEFAULT_ORIGINS)
    for url in ("http://localhost:3000", "http://127.0.0.1:5173/", "http://[::1]:80/"):
        assert origin_allowed(origin_of(url), DEFAULT_ORIGINS)
    for url in (
        "https://localhost:3000",  # another scheme is another origin
        "http://evil.localhost:3000",
        "http://localhost.evil.test",
        "http://127.0.0.2:3000",
        "http://0.0.0.0:3000",
        "http://127.1:3000",
    ):
        assert not origin_allowed(origin_of(url), DEFAULT_ORIGINS), url


def test_a_port_is_matched_exactly_unless_any():
    patterns = ["https://staging.example.com:443", "http://localhost:3000"]
    assert origin_allowed("https://staging.example.com:443", patterns)
    assert not origin_allowed("https://staging.example.com:8443", patterns)
    assert origin_allowed("http://localhost:3000", patterns)
    assert not origin_allowed("http://localhost:3001", patterns)
    assert not origin_allowed(None, patterns)
    assert not origin_allowed("http://localhost:3000", [])


# ── the class of a call ─────────────────────────────────────────────────────


def test_localhost_dev_server_is_write():
    assert _cls("navigate", {"url": DEV}) is WRITE
    assert _cls("click", {"selector": "#buy"}, last="http://localhost:3000") is WRITE
    assert _cls("type", {"text": "hello"}, last="http://127.0.0.1:5173") is WRITE
    assert _cls("zoom", {"level": 2}, last=DEV) is WRITE  # any other action


def test_any_other_origin_is_destructive():
    assert _cls("navigate", {"url": "https://example.com/"}) is DESTRUCTIVE
    assert _cls("click", {"selector": "a"}, last="https://example.com:443") is DESTRUCTIVE
    assert _cls("screenshot", {}, last="https://example.com:443") is DESTRUCTIVE


@pytest.mark.parametrize(
    "url",
    ["file:///etc/passwd", "data:text/html,hi", "javascript:fetch('//evil.test')", "about:blank"],
)
def test_file_data_javascript_are_destructive(url):
    assert _cls("navigate", {"url": url}, last=DEV) is DESTRUCTIVE


def test_unknown_origin_before_any_navigate_is_destructive():
    assert _cls("click", {"selector": "#buy"}) is DESTRUCTIVE
    assert _cls("screenshot") is DESTRUCTIVE


def test_screenshot_on_localhost_is_read():
    assert _cls("screenshot", {}, last=DEV) is READ
    assert _cls("take_snapshot", {}, last=DEV) is READ
    assert _cls("get_text", {"selector": "h1"}, last=DEV) is READ


def test_on_an_allowed_origin_the_name_decides_and_never_loosens():
    assert _cls("close_tab", {}, last=DEV) is DESTRUCTIVE  # a destructive word stays
    assert _cls("delete_cookies", {}, last=DEV) is DESTRUCTIVE
    assert _cls("check", {"selector": "#agree"}, last=DEV) is WRITE  # ticks a box
    assert _cls("download", {"selector": "a"}, last=DEV) is WRITE  # saves a file here
    assert _cls("screenshot_and_click", {}, last=DEV) is WRITE
    assert _cls("bad name!", {}, last=DEV) is DESTRUCTIVE  # not a capability id


@pytest.mark.parametrize(
    "name",
    ["evaluate", "browser_evaluate", "browser_run_code", "run_script", "execJavascript", "eval_js"],
)
def test_a_call_that_runs_code_is_destructive_on_any_origin(name):
    moves = {"expression": "location = 'https://bank.example/'"}
    assert _cls(name, moves, last=DEV) is DESTRUCTIVE  # it can act anywhere, and send anything
    assert class_label(_browser(), _tool(name)) == "destructive"


def test_hints_and_settings_only_tighten():
    conn = _browser(overrides={"screenshot": "write"})
    assert classify(conn, _tool("screenshot"), {}, DEV) is WRITE
    assert classify(_browser(), _tool("screenshot", destructive_hint=True), {}, DEV) is DESTRUCTIVE
    assert classify(_browser(), _tool("click", read_only_hint=True), {}, DEV) is WRITE


def test_a_url_under_another_name_counts():
    for key in ("href", "uri", "link", "location", "address", "targetUrl", "page_url", "URL"):
        assert _cls("go", {key: "https://evil.test/"}, last=DEV) is DESTRUCTIVE, key
        assert _cls("go", {key: DEV}, last="https://evil.test:443") is WRITE, key


@pytest.mark.parametrize(
    "args",
    [
        {"urls": [DEV, "https://bank.example/"]},
        {"links": ["https://bank.example/"]},
        {"urls": "https://bank.example/"},
        {"urls": [3]},
        {"options": {"url": "https://bank.example/"}},
        {"steps": [{"action": "goto", "href": "https://bank.example/"}]},
        {"a": [[{"b": {"targetUrl": "https://bank.example/"}}]]},
    ],
)
def test_a_url_under_a_plural_or_nested_key_counts(args):
    assert _cls("navigate", args, last=DEV) is DESTRUCTIVE


def test_allowed_urls_under_a_plural_or_nested_key_are_where_the_call_acts():
    elsewhere = "https://evil.test:443"
    assert _cls("navigate", {"urls": [DEV, "http://127.0.0.1:5173/"]}, last=elsewhere) is WRITE
    assert _cls("navigate", {"options": {"url": DEV}}, last=elsewhere) is WRITE
    assert _cls("navigate", {"urls": []}, last=DEV) is WRITE  # none given: where the page is


def test_a_deeply_nested_url_is_found_without_running_out_of_stack():
    args: dict = {}
    inner = args
    for _ in range(20000):
        inner["n"] = {}
        inner = inner["n"]
    inner["url"] = "https://bank.example/"
    assert _cls("navigate", args, last=DEV) is DESTRUCTIVE


def test_every_url_in_a_call_must_be_allowed():
    args = {"url": DEV, "referrer_url": "https://evil.test/"}
    assert _cls("navigate", args, last=DEV) is DESTRUCTIVE
    assert _cls("navigate", {"url": DEV, "href": "http://127.0.0.1:3000"}) is WRITE


def test_a_url_that_is_not_text_is_destructive():
    assert _cls("navigate", {"url": None}, last=DEV) is DESTRUCTIVE
    assert _cls("navigate", {"url": [DEV]}, last=DEV) is DESTRUCTIVE


def test_a_person_can_allow_another_origin():
    origins = [*DEFAULT_ORIGINS, "https://staging.example.com:443"]
    assert _cls("navigate", {"url": "https://staging.example.com/x"}, origins=origins) is WRITE
    assert _cls("navigate", {"url": "https://example.com/"}, origins=origins) is DESTRUCTIVE


def test_a_connection_that_is_not_a_browser_is_classed_as_before():
    plain = Connection(id="did:orcha:agent:conn-000000000002", name="gh", transport="stdio")
    assert classify(plain, _tool("create_issue"), {"url": "https://evil.test"}, None) is WRITE
    assert classify(plain, _tool("list_issues")) is READ
    assert classify(plain, _tool("navigate"), {"url": DEV}, DEV) is DESTRUCTIVE  # unrecognised


def test_a_tool_is_labelled_by_its_class_on_the_allowed_origins():
    where = ", ".join(DEFAULT_ORIGINS)
    assert class_label(_browser(), _tool("screenshot")) == f"read on {where}; destructive elsewhere"
    assert class_label(_browser(), _tool("click")) == f"write on {where}; destructive elsewhere"
    assert class_label(_browser(), _tool("close_tab")) == "destructive"
    assert (
        class_label(_browser(overrides={"click": "destructive"}), _tool("click")) == "destructive"
    )


@pytest.mark.parametrize(
    "args",
    [
        {"targets": ["https://evil.example/"]},
        {"targets": "https://evil.example/"},
        {"target": "https://evil.example/"},
        {"page": {"url": "https://evil.example/"}},
        {"steps": [{"targets": ["http://127.0.0.1:3000/", "https://bank.example/"]}]},
    ],
)
def test_a_url_under_targets_or_a_nested_page_is_where_the_call_acts(args):
    assert _cls("navigate", args, last=DEV) is DESTRUCTIVE


def test_allowed_targets_are_where_the_call_acts():
    elsewhere = "https://evil.example:443"
    assert _cls("navigate", {"targets": [DEV, "http://127.0.0.1:5173/"]}, last=elsewhere) is WRITE
    assert _cls("navigate", {"page": {"url": DEV}}, last=elsewhere) is WRITE


@pytest.mark.parametrize(
    "url",
    [
        "http://2130706433/",
        "http://2130706433:3000/",
        "http://0x7f000001/",
        "http://0x7f.0.0.1/",
        "http://0177.0.0.1/",
        "http://127.1/",
        "http://127.0.1/",
        "http://0x7f.0x0.0x0.0x1/",
        "http://127.0.0.0x1/",
        "http://017700000001/",
    ],
)
def test_a_host_a_browser_may_read_as_another_address_has_no_origin(url):
    assert origin_of(url) is None
    assert _cls("navigate", {"url": url}, last=DEV) is DESTRUCTIVE
    assert _cls("screenshot", {"url": url}, last=DEV) is DESTRUCTIVE


def test_a_dotted_quad_keeps_its_origin():
    assert origin_of("http://127.0.0.1:3000/a") == "http://127.0.0.1:3000"
    assert origin_of("http://8.8.8.8/") == "http://8.8.8.8:80"
    assert origin_allowed(origin_of("http://8.8.8.8/"), DEFAULT_ORIGINS) is False
    assert origin_of("http://[::1]:8080/") == "http://[::1]:8080"
    assert origin_of("http://[0::1]/") == "http://[::1]:80"


@pytest.mark.parametrize(
    "url",
    [
        "http://[::ffff:127.0.0.1]/",
        "http://[::ffff:7f00:1]/",
        "http://[0:0:0:0:0:ffff:127.0.0.1]/",
        "http://[::ffff:8.8.8.8]/",
        "http://[::ffff:127.0.0.1]:3000/",
    ],
)
def test_an_ipv4_address_written_inside_ipv6_has_no_origin(url):
    assert origin_of(url) is None
    assert _cls("navigate", {"url": url}, last=DEV) is DESTRUCTIVE


@pytest.mark.parametrize(
    "url",
    [
        "http://[::1%25eth0]/",
        "http://[::1%1]/",
        "http://[fe80::1%25eth0]/",
    ],
)
def test_a_percent_sign_in_an_ipv6_host_has_no_origin(url):
    assert origin_of(url) is None
    assert _cls("navigate", {"url": url}, last=DEV) is DESTRUCTIVE


@pytest.mark.parametrize(
    ("url", "origin"),
    [
        ("http://localhost.:3000/a", "http://localhost:3000"),
        ("http://LOCALHOST.:3000/", "http://localhost:3000"),
        ("HTTP://127.0.0.1./", "http://127.0.0.1:80"),
        ("https://Evil.Example./x", "https://evil.example:443"),
    ],
)
def test_one_trailing_dot_is_the_same_host(url, origin):
    assert origin_of(url) == origin


def test_a_second_trailing_dot_is_not_the_allowed_host():
    assert not origin_allowed(origin_of("http://localhost../"), DEFAULT_ORIGINS)


@pytest.mark.parametrize(
    "url",
    [
        "http://0x/",
        "http://0X/",
        "http://1.0x/",
        "http://256.1.1.1/",
        "http://1.2.3.4.5/",
        "http://localhost../",
    ],
)
def test_more_host_spellings_have_no_origin(url):
    assert origin_of(url) is None
    assert _cls("navigate", {"url": url}, last=DEV) is DESTRUCTIVE


@pytest.mark.parametrize("url", ["http://example.123/", "http://foo.0x1/", "http://foo.0X/"])
def test_a_host_whose_last_label_is_a_number_has_no_origin(url):
    assert origin_of(url) is None
    assert _cls("navigate", {"url": url}, last=DEV) is DESTRUCTIVE


@pytest.mark.parametrize(
    ("url", "origin"),
    [
        ("http://localhost/", "http://localhost:80"),
        ("http://127.0.0.1/", "http://127.0.0.1:80"),
        ("http://10.0.0.255/", "http://10.0.0.255:80"),
        ("http://example.com/", "http://example.com:80"),
        ("http://a-b.example/", "http://a-b.example:80"),
        ("http://0xdeadbeef.com/", "http://0xdeadbeef.com:80"),
        ("http://123.example/", "http://123.example:80"),
        ("http://[::1]/", "http://[::1]:80"),
    ],
)
def test_a_host_browsers_share_keeps_its_origin(url, origin):
    assert origin_of(url) == origin


def test_userinfo_is_classed_as_the_host_after_the_at():
    hidden = "http://127.0.0.1:3000@evil.example/"
    assert origin_of(hidden) == "http://evil.example:80"
    assert _cls("navigate", {"url": hidden}, last=DEV) is DESTRUCTIVE
    local = "http://evil.example@127.0.0.1:3000/"
    assert origin_of(local) == "http://127.0.0.1:3000"
    assert _cls("navigate", {"url": local}) is WRITE


@pytest.mark.parametrize(
    "url",
    [
        r"http:\\evil.example",
        r"http:\\evil.example/",
        "//evil.example",
        "//evil.example/x",
        "http://evil.example\t/",
        "http://evil.example\n@localhost/",
        "http://local%68ost/",
        "http://127.0.0.1%2e.evil.example/",
        "javascript:alert(1)",
        "JavaScript:alert(1)",
        "data:text/html,hi",
        "file:///etc/passwd",
        "blob:http://localhost:3000/1",
        "view-source:http://localhost:3000/",
        "http://localhost:65536/",
        "http://127.0.0.1:0xbb8/",
        "http://bücher.example/",
    ],
)
def test_a_url_a_browser_could_read_differently_has_no_origin(url):
    assert origin_of(url) is None
    assert _cls("navigate", {"url": url}, last=DEV) is DESTRUCTIVE


def test_port_zero_on_this_machine_is_still_this_machine():
    assert origin_of("http://localhost:0/") == "http://localhost:0"
    assert origin_allowed(origin_of("http://127.0.0.1:0/"), DEFAULT_ORIGINS)
    assert origin_of("http://localhost:65535/") == "http://localhost:65535"


def test_punycode_is_the_host_as_written():
    assert origin_of("http://xn--n3h.example/") == "http://xn--n3h.example:80"
    assert not origin_allowed(origin_of("http://xn--n3h.example/"), DEFAULT_ORIGINS)


@pytest.mark.parametrize(
    "name",
    ["evaluate", "run_script", "execute_js", "addInitScript", "execJavascript"],
)
def test_a_call_that_runs_code_in_the_page_is_destructive(name):
    assert _cls(name, {"expression": "1"}, last=DEV) is DESTRUCTIVE
    assert _cls(name, {}, last=DEV) is DESTRUCTIVE


@pytest.mark.parametrize(
    "url", ["http://1.2.3.²/", "http://1.2.3.①/", "http://²/", "http://١.٢.٣.٤/"]
)
def test_a_host_with_a_digit_outside_ascii_has_no_origin_and_never_raises(url):
    # isdigit() is true for ² and ①, which int() refuses; a raise here would leave no record
    assert origin_of(url) is None
    assert _cls("navigate", {"url": url}, last=DEV) is DESTRUCTIVE


def test_the_ipv4_checks_read_ascii_digits_only():
    from metabot import origins

    for label in ("²", "①", "٣", "1²"):
        assert origins._ipv4_part(label) is None
        assert not origins._label_is_a_number(label)
    assert not origins._four_plain_octets("1.2.3.²")
    assert not origins._ipv4_spelled_another_way("1.2.3.²")
