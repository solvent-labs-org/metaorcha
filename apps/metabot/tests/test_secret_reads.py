"""S2: a call that names MetaBot's secrets, its signing key or a login folder always asks.

Asked when a person is there, refused when no one is (a routine). Before, ``cat`` of a
connection token, a claude ``Read`` of the signing key and ``grep -r`` over the home
folder ran with no one asked, and a write over the key ran wherever writes were allowed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fake_chat import answer as _answer
from fake_chat import call as _call
from metabot.approvals import DenyAll, Scripted
from metabot.checks import refused
from metabot.config import Config, save_config
from metabot.connections import Connection
from metabot.engines.openai_compat import OpenAIEngine
from metabot.gate import Policy
from metabot.hook import triage
from metabot.receipts import ensure_key
from metabot.runner import run_job
from metabot.scope_classes import ScopeClass
from metabot.shell_policy import classify_engine_tool, classify_shell
from metabot.switchboard import classify

READ, WRITE, DESTRUCTIVE = ScopeClass.READ, ScopeClass.WRITE, ScopeClass.DESTRUCTIVE
TOKEN = "conn-token-7f3a"


@pytest.fixture
def user(tmp_path, monkeypatch) -> Path:
    """A person's home: MetaBot's home and the signing key where they are by default, and
    one login folder."""
    u = tmp_path / "u"
    monkeypatch.setenv("HOME", str(u))
    monkeypatch.setenv("METABOT_HOME", str(u / ".metabot"))
    monkeypatch.delenv("ORCHA_KEY_PATH", raising=False)
    (u / ".metabot" / "secrets").mkdir(parents=True)
    (u / ".metabot" / "secrets" / "github").write_text(TOKEN)
    ensure_key()  # made where a run makes it: ~/.orcha/key
    (u / ".claude-work").mkdir(mode=0o700)
    (u / "proj" / "src").mkdir(parents=True)
    (u / "notes.txt").write_text("")
    save_config(Config(logins={"work": {"engine": "claude", "home": str(u / ".claude-work")}}))
    return u


# ── the shell ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "command",
    [
        "cat ~/.metabot/secrets/github",
        "cat $HOME/.metabot/secrets/github",
        'cat "${HOME}/.metabot/secrets/github"',
        "head -c 64 ~/.orcha/key",
        "cat < ~/.orcha/key",
        "cat ~/.claude-work/.credentials.json",
        "ls ~/.metabot/secrets",
        "cat ~/.metabot/secr*/github",
        "cat ~/.metabot/{secrets,x}/github",
        "cat ~/.*/key",
        "cat $METABOT_HOME/secrets/github",
        "python3 -c \"print(open('{u}/.orcha/key').read())\"",
        "sh -c 'cat ~/.orcha/key'",
        "cat $(echo ~/.metabot/secrets/github)",
        "cat ../.metabot/secrets/github",
        "cat file://{u}/.orcha/key",
        "base64 --input={u}/.orcha/key",
    ],
)
def test_a_line_that_names_a_secret_always_asks(user, command):
    line = command.replace("{u}", str(user))
    assert classify_shell(line, cwd=str(user / "proj")) is DESTRUCTIVE  # before: read or write


@pytest.mark.parametrize(
    "command",
    [
        "echo hi > ~/.orcha/key",  # before: write, so it ran wherever writes were allowed
        "cp ~/.orcha/key ./k",
        "mv ~/.metabot/secrets/github /tmp/x",
    ],
)
def test_a_write_or_copy_of_a_secret_always_asks(user, command):
    assert classify_shell(command, cwd=str(user / "proj")) is DESTRUCTIVE


@pytest.mark.parametrize(
    "command",
    [
        "grep -r token ~",
        "grep -rn token ~/.metabot",
        "time grep -R token ~",
        "rg token ~",
        "cp -r ~ /tmp/all",
        "tar czf all.tgz -C ~ .",
        "cd ~ && cat .metabot/secrets/github",
        "cd && cat .orcha/key",
        "X=~/.metabot; cat $X/secrets/github",
    ],
)
def test_reading_through_or_copying_a_folder_that_holds_a_secret_asks(user, command):
    assert classify_shell(command, cwd=str(user / "proj")) is DESTRUCTIVE


def test_a_recursive_read_with_no_folder_reads_the_one_it_is_in(user):
    assert classify_shell("grep -r token", cwd=str(user)) is DESTRUCTIVE
    assert classify_shell("rg token", cwd=str(user)) is DESTRUCTIVE
    assert classify_shell("grep -r token", cwd=str(user / "proj")) is READ


def test_a_link_to_a_secret_is_the_secret(user):
    (user / "proj" / "s").symlink_to(user / ".metabot" / "secrets")
    assert classify_shell("cat s/github", cwd=str(user / "proj")) is DESTRUCTIVE


def test_a_relative_path_is_read_from_the_folder_the_line_runs_in(user):
    assert classify_shell("cat secrets/github", cwd=str(user / ".metabot")) is DESTRUCTIVE
    assert classify_shell("cat secrets/github", cwd=str(user / "proj")) is READ


@pytest.mark.parametrize(
    ("command", "cls"),
    [
        ("ls ~", READ),  # names only, and no secret's name is one
        ("ls ~/.metabot", READ),
        ("ls ~/*", READ),  # a glob that starts with no dot never matches .metabot
        ("find ~ -name '*.py'", READ),
        ("du -sh ~", READ),
        ("cat ~/notes.txt", READ),
        ("grep -rn token src", READ),
        ("cat src/a.py", READ),
        ("cp src/a.py src/b.py", WRITE),
        ("echo $PATH", READ),
        ("rm -rf build", DESTRUCTIVE),
    ],
)
def test_lines_that_name_no_secret_keep_their_class(user, command, cls):
    assert classify_shell(command, cwd=str(user / "proj")) is cls


def test_the_signing_key_is_found_where_orcha_key_path_puts_it(user, tmp_path, monkeypatch):
    elsewhere = tmp_path / "keys" / "mine"
    monkeypatch.setenv("ORCHA_KEY_PATH", str(elsewhere))
    assert classify_shell(f"cat {elsewhere}", cwd=str(user)) is DESTRUCTIVE
    assert classify_shell("cat ~/.orcha/key", cwd=str(user / "proj")) is READ


def test_a_login_added_later_is_guarded_at_once(user):
    later = user / ".codex-2"
    assert classify_shell(f"cat {later}/auth.json", cwd=str(user)) is READ
    save_config(Config(logins={"two": {"engine": "codex", "home": str(later)}}))
    assert classify_shell(f"cat {later}/auth.json", cwd=str(user)) is DESTRUCTIVE


def test_a_config_that_does_not_read_makes_every_line_ask(user):
    (user / ".metabot" / "config.json").write_text("{not json")
    assert classify_shell("cat src/a.py", cwd=str(user / "proj")) is DESTRUCTIVE


def test_a_check_that_names_the_key_never_runs(user):
    assert refused({"done:leak": "cat ~/.orcha/key", "done:tests": "pytest -q"}) == {
        "done:leak": "destructive"
    }


# ── an engine's own tools ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("Read", {"file_path": "{u}/.orcha/key"}),
        ("Read", {"file_path": "~/.metabot/secrets/github"}),
        ("Read", {"file_path": "../.metabot/secrets/github"}),
        ("Write", {"file_path": "{u}/.orcha/key", "content": "x"}),
        ("Edit", {"file_path": "{u}/.claude-work/settings.json"}),
        ("NotebookRead", {"notebook_path": "{u}/.claude-work/n.ipynb"}),
        ("LS", {"path": "{u}/.metabot/secrets"}),
        ("Grep", {"pattern": "tok", "path": "{u}"}),
        ("Glob", {"pattern": "**/*", "path": "{u}/.metabot"}),
        ("Glob", {"pattern": "{u}/.orcha/*"}),
        ("WebFetch", {"url": "file://{u}/.orcha/key", "prompt": "x"}),
        ("mcp__files__read_file", {"path": "{u}/.orcha/key"}),
        ("Bash", {"command": "cat ~/.orcha/key"}),
    ],
)
def test_an_engine_tool_that_names_a_secret_always_asks(user, tool, args):
    filled = {k: v.replace("{u}", str(user)) for k, v in args.items()}
    assert classify_engine_tool(tool, filled, cwd=str(user / "proj")) is DESTRUCTIVE


@pytest.mark.parametrize(
    ("tool", "args", "cls"),
    [
        ("Read", {"file_path": "{u}/proj/src/a.py"}, READ),
        ("Grep", {"pattern": "tok", "path": "{u}/proj"}, READ),
        ("Grep", {"pattern": "~/.orcha/key"}, READ),  # a pattern is not a path
        ("LS", {"path": "{u}"}, READ),
        ("Glob", {"pattern": "*.md", "path": "{u}"}, READ),
        ("Write", {"file_path": "{u}/proj/README.md", "content": "never ~/.orcha/key"}, WRITE),
        ("Task", {"prompt": "do not read ~/.orcha/key"}, READ),
    ],
)
def test_an_engine_tool_that_names_no_secret_keeps_its_class(user, tool, args, cls):
    filled = {k: v.replace("{u}", str(user)) for k, v in args.items()}
    assert classify_engine_tool(tool, filled, cwd=str(user / "proj")) is cls


def test_grep_with_no_folder_searches_the_one_it_runs_in(user):
    assert classify_engine_tool("Grep", {"pattern": "tok"}, cwd=str(user)) is DESTRUCTIVE


def test_the_gate_asks_the_approver_for_a_read_of_the_key(user):
    """The gate used to allow every read itself; this one goes to the approver."""
    payload = {
        "tool_name": "Read",
        "tool_input": {"file_path": "../.orcha/key"},  # read where the engine runs
        "tool_use_id": "t1",
        "cwd": str(user / "proj"),
    }

    ask = triage(payload, "claude-code")  # the runner's side of the gate (G1)
    assert ask["op"] == "approve"
    assert ask["cls"] == "destructive" and ask["tool"] == "claude-code/Read"


# ── the openai engine's built-in tools, in a routine ────────────────────────


async def test_a_routine_never_reads_a_secret_through_the_openai_engine(user, chat):
    fake = chat(
        [
            _call("read_file", {"path": ".metabot/secrets/github"}, "c1"),
            _call("shell", {"command": "cat .orcha/key"}, "c2"),
            _answer("done"),
        ]
    )
    engine = OpenAIEngine(base_url=fake.url, model="tiny", key_env=None, config=Config())
    result = await run_job(
        engine=engine,
        prompt="go",
        cwd=user,  # a run in the home folder, where both are inside its folder
        policy=Policy(allow_write=True, interactive=False),
        approver=DenyAll(),
    )
    steps = json.loads(result.receipt.read_text())["steps"]
    assert [s["success"] for s in steps] == [False, False]
    sent = json.dumps(fake.requests)
    key = (user / ".orcha" / "key").read_text().strip()
    assert TOKEN not in sent and key not in sent


async def test_a_person_is_asked_before_the_openai_engine_reads_a_login_folder(user, chat):
    (user / ".claude-work" / "settings.json").write_text("{}")
    fake = chat([_call("list_dir", {"path": ".claude-work"}, "c1"), _answer("done")])
    engine = OpenAIEngine(base_url=fake.url, model="tiny", key_env=None, config=Config())
    approver = Scripted([True])
    await run_job(engine=engine, prompt="go", cwd=user, policy=Policy(), approver=approver)
    assert approver.asked == 1  # before: a read, so no one was asked


# ── a connection's tools ────────────────────────────────────────────────────


def test_a_connection_call_that_names_a_secret_always_asks(user):
    from mcp.types import Tool

    conn = Connection(id="did:orcha:agent:conn-0000000000aa", name="files", transport="stdio")
    tool = Tool(name="read_file", input_schema={"type": "object"})
    assert classify(conn, tool, {"path": str(user / "proj" / "a.py")}) is READ
    assert classify(conn, tool, {"path": str(user / ".orcha" / "key")}) is DESTRUCTIVE
    assert classify(conn, tool, {"paths": ["~/.metabot/secrets/github"]}) is DESTRUCTIVE


@pytest.mark.parametrize(
    "command",
    [
        'cat "{h}/settings.json"',
        "cat '{h}'/settings.json",
        "sh -c 'cat \"{h}/settings.json\"'",
        'cd "{h}" && cat settings.json',
    ],
)
def test_a_login_folder_with_a_space_is_one_path(user, command):
    spaced = user / "my logins" / "work"
    spaced.mkdir(parents=True)
    save_config(Config(logins={"work": {"engine": "claude", "home": str(spaced)}}))
    line = command.replace("{h}", str(spaced))
    assert classify_shell(line, cwd=str(user / "proj")) is DESTRUCTIVE


def test_the_key_is_found_where_the_signer_finds_it(user, tmp_path, monkeypatch):
    from emerge.record import resolve_key_path

    from metabot.secret_paths import key_path

    assert key_path() == str(resolve_key_path())
    monkeypatch.setenv("ORCHA_KEY_PATH", " ~/keys/k ")
    assert key_path() == str(resolve_key_path())


@pytest.mark.parametrize(
    ("command", "cls"),
    [
        ("echo $$ > pgid", WRITE),
        ("sleep 30 & echo $! > child", WRITE),
        ("echo $1", READ),
        ("rm\x00 -rf x", WRITE),
    ],
)
def test_a_word_that_names_no_path_is_no_secret_even_in_the_folder_that_holds_one(
    user, command, cls
):
    # a variable the line makes as it runs, or a word no file can be named, names no path
    assert classify_shell(command, cwd=str(user)) is cls


# ── review 1 (11-02): spellings that reached a secret ───────────────────────


@pytest.mark.parametrize(
    "command",
    [
        "cat ~/*/../.orcha/key",  # a pattern, then .. back out of it
        "cat ~/pro?/../.orcha/key",
        "cat ~/*/../.claude-work/.credentials.json",
        "cat ~/{.orcha,x}/key",  # braces are alternatives, not a pattern
        "cat ~/{.metabot,x}/secrets/github",
        "cat ~/$''.orcha/key",
        "cat ~/$'.orcha'/key",
        "cat $'{u}/.orcha/key'",
        "cat ${HOME:-x}/.orcha/key",
        "cat {~,/x}/.orcha/key",
        "cat /proc/self/cwd/../.orcha/key",
        "cat ~+/../.orcha/key",
        "cat ~/.or\\\ncha/key",  # a line continued inside a word
        'for d in ~; do cat "$d/.orcha/key"; done',
        "grep -d recurse conn-token ~",
        "grep --directories=recurse conn-token ~",
        "grep --rec conn-token ~",
        "cd src && cat ../../.orcha/key",
        "cd -- && cat .orcha/key",
        "cd -P && cat .orcha/key",
        "cat $(pwd)/../.orcha/key",
        "cat ~/$(echo .orcha)/key",
        "X=.orcha; cat ~/$X/key",
        "find ~/.orcha -type f | xargs cat",
        "find ~/.orcha -print0 | sort --files0-from=-",
        "find ~/.orcha -exec cat {} +",
        "bash <<'EOF'\ncat ~/.orcha/key\nEOF",
        "python3 - <<'EOF'\nprint(open('{u}/.orcha/key').read())\nEOF",
    ],
)
def test_a_spelling_that_reaches_a_secret_asks(user, command):
    line = command.replace("{u}", str(user))
    assert classify_shell(line, cwd=str(user / "proj")) is DESTRUCTIVE


def test_a_line_s_folder_is_its_own_not_the_classifier_s(user, monkeypatch):
    monkeypatch.setenv("PWD", str(user / "proj" / "src"))
    assert classify_shell("cat $PWD/../.orcha/key", cwd=str(user / "proj")) is DESTRUCTIVE
    assert classify_shell("cat /proc/self/cwd/.orcha/key", cwd=str(user)) is DESTRUCTIVE


@pytest.mark.parametrize(
    ("command", "cls"),
    [
        ("cp dist/report.pdf ~/", WRITE),  # into a folder that holds one, not out of it
        ("mv build.log ~", WRITE),
        ('echo "$HOME" > home.txt', WRITE),
        ("cd .. && git status", WRITE),
        ("git -C .. log --oneline -3", WRITE),
        ("grep -n '$HOME' src/a.py", READ),  # quoted: the shell expands nothing in it
        ('rg -n "~/.orcha/key" src', READ),
        ('git commit -m "never reads {u}/.orcha/key"', WRITE),
        ("cat <<'EOF' > notes.md\nthe key is ~/.orcha/key; it's never read\nEOF", WRITE),
        ("ls *.py | head", READ),
        ("find . -name '*.py' | xargs grep -n token", WRITE),  # xargs runs a program
        ("echo $PATH", READ),
    ],
)
def test_ordinary_work_near_a_secret_keeps_its_class(user, command, cls):
    line = command.replace("{u}", str(user))
    assert classify_shell(line, cwd=str(user / "proj")) is cls


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("Read", {"file_path": "{u}/*/../.orcha/key"}),  # a tool opens a path as written
        ("Read", {"file_path": "/proc/self/cwd/../.orcha/key"}),
        ("Read", {"file_path": "{u}/$x/../.orcha/key"}),
        ("Write", {"file_path": "{u}/*/../.orcha/key", "content": "x"}),
        ("Edit", {"file_path": "{u}/{a}/../.orcha/key"}),
    ],
)
def test_a_tool_path_is_read_as_the_tool_opens_it(user, tool, args):
    filled = {k: v.replace("{u}", str(user)) for k, v in args.items()}
    assert classify_engine_tool(tool, filled, cwd=str(user / "proj")) is DESTRUCTIVE


def test_grep_with_an_empty_folder_searches_the_one_it_runs_in(user):
    assert classify_engine_tool("Grep", {"pattern": "t", "path": ""}, cwd=str(user)) is DESTRUCTIVE


@pytest.mark.parametrize(
    "path",
    ["*/../.orcha/key", "$Z/../.orcha/key", "{x}/../.metabot/secrets/github"],
)
async def test_the_openai_engine_never_opens_a_secret_by_a_path_that_looks_like_a_pattern(
    user, chat, path
):
    before = (user / ".orcha" / "key").read_bytes()
    fake = chat(
        [
            _call("read_file", {"path": path}, "c1"),
            _call("write_file", {"path": path, "content": "attacker"}, "c2"),
            _answer("done"),
        ]
    )
    engine = OpenAIEngine(base_url=fake.url, model="tiny", key_env=None, config=Config())
    result = await run_job(
        engine=engine,
        prompt="go",
        cwd=user,
        policy=Policy(allow_write=True, interactive=False),
        approver=DenyAll(),
    )
    steps = json.loads(result.receipt.read_text())["steps"]
    assert [s["success"] for s in steps] == [False, False]
    assert (user / ".orcha" / "key").read_bytes() == before
    assert TOKEN not in json.dumps(fake.requests)


@pytest.mark.parametrize(
    "args",
    [
        {"items": ["x"] * 300 + ["{u}/.orcha/key"]},  # past the scan's reach: asked
        {"a": {"b": {"c": {"d": {"e": {"f": {"g": {"h": {"i": {"j": "{u}/.orcha/key"}}}}}}}}}},
        {"{u}/.orcha/key": "read"},
        {"path": "{u}/x/../.orcha/key"},
    ],
)
def test_a_connection_call_that_could_name_a_secret_asks(user, args):
    from mcp.types import Tool

    filled = json.loads(json.dumps(args).replace("{u}", str(user)))
    conn = Connection(id="did:orcha:agent:conn-0000000000aa", name="files", transport="stdio")
    tool = Tool(name="read_file", input_schema={"type": "object"})
    assert classify(conn, tool, filled) is DESTRUCTIVE


# ── review 1, round 2 (11-02): what the rewrite reads that the first one did not ───


@pytest.mark.parametrize(
    "command",
    [
        "echo ~/.orcha | xargs -I{} cat {}/key",  # names handed on from a folder above one
        "xargs cat <<EOF\n{u}/.orcha/key\nEOF",  # names in a here-document a command takes
        'while read -r f; do cat "$f"; done <<EOF\n{u}/.orcha/key\nEOF',
        "cat ~/proj/.*/.orcha/key",  # `.*` matches `..` in older shells
        "shopt -s dotglob; cat ~/*/key",
        "cat ~/.orcha/ke[y]",
        "cat ~/.orch{a..b}/key",
        "env -C ~ cat .orcha/key",
        "source ./env.sh && cat .orcha/key",  # a sourced file may move the line anywhere
        "cd - && cat .orcha/key",
        "shopt -s extglob; cat ~/.orcha/k@(e)y",
        "cat $PWD:h/.orcha/key",  # zsh reads :h as the parent folder
        "find . -exec sh -c 'cat ~/.orcha/key' \\;",
        'cat "$(printf %s ~/.orcha/key)"',
        "cat ~/proj/l*/key",  # a pattern that matches a link to a guarded folder
        "cat lp/../.orcha/key",  # a link to /proc/self/cwd: the line's folder, not ours
        "while true; do cd ..; done & cat .orcha/key",  # a move made again and again
        "echo ~+/../.orcha/key",  # ~+ is the line's folder
    ],
)
def test_a_spelling_found_on_review_round_two_asks(user, command):
    (user / "proj" / "link").symlink_to(user / ".orcha")
    (user / "proj" / "lp").symlink_to("/proc/self/cwd")
    line = command.replace("{u}", str(user))
    assert classify_shell(line, cwd=str(user / "proj")) is DESTRUCTIVE


@pytest.mark.parametrize(
    ("command", "cls"),
    [
        ('for f in src/*.py; do wc -l "$f"; done', READ),  # the loop's words are known
        ('grep -rn "$1" src', READ),  # a pattern is not a path
        ('head -n "$N" src/a.py', READ),  # nor is a count
        ("export X=$HOME", WRITE),
        ("ls ~ | head", READ),  # names, and nothing takes them as paths
        ("cat ~/notes.txt ~/proj/src/a.py", READ),
        ("cd src && cat a.py", WRITE),  # cd is a write; one move, never one from each
        ("cd src; cd ../src && head -5 a.py", WRITE),
        ('for d in a b; do cat "src/$d.py"; done', READ),
    ],
)
def test_ordinary_work_found_on_review_round_two_keeps_its_class(user, command, cls):
    assert classify_shell(command, cwd=str(user / "proj")) is cls


@pytest.mark.parametrize(
    ("tool", "args", "cls"),
    [
        ("Glob", {"pattern": "**/*", "path": "{u}"}, DESTRUCTIVE),  # can name a path in one
        ("Glob", {"pattern": "*", "path": "{u}"}, DESTRUCTIVE),  # names the login folder
        ("Glob", {"pattern": "*", "path": "{u}/proj"}, READ),
        ("Glob", {"pattern": "**/*.{py,md}"}, READ),
        ("Glob", {"pattern": "**/github", "path": "{u}"}, DESTRUCTIVE),  # ** spans folders
        ("Read", {"file_path": "{u}/proj/lp/../.orcha/key"}, DESTRUCTIVE),
        ("Read", {"file_path": "file://localhost{u}/%2Eorcha/key"}, DESTRUCTIVE),
        ("LS", {"path": "{u}/.metabot"}, READ),
    ],
)
def test_an_engine_tool_found_on_review_round_two(user, tool, args, cls):
    (user / "proj" / "lp").symlink_to("/proc/self/cwd")
    filled = {k: v.replace("{u}", str(user)) for k, v in args.items()}
    assert classify_engine_tool(tool, filled, cwd=str(user / "proj")) is cls


def test_a_connection_call_past_the_scan_s_reach_asks_with_no_secret_in_it(user):
    from mcp.types import Tool

    from metabot.secret_paths import MAX_DEPTH, MAX_LEAVES

    conn = Connection(id="did:orcha:agent:conn-0000000000aa", name="files", transport="stdio")
    tool = Tool(name="read_file", input_schema={"type": "object"})
    deep: dict = {"path": "a.txt"}
    for _ in range(MAX_DEPTH + 1):
        deep = {"x": deep}
    assert classify(conn, tool, {"items": ["x"] * (MAX_LEAVES - 1)}) is READ  # a key is one
    assert classify(conn, tool, {"items": ["x"] * (MAX_LEAVES + 1)}) is DESTRUCTIVE
    assert classify(conn, tool, deep) is DESTRUCTIVE
    assert classify(conn, tool, {"command": "cat ~/.orcha/key"}) is DESTRUCTIVE
    assert classify(conn, tool, {"body": "see https://example.com/a/b"}) is READ


def test_a_move_inside_a_loop_may_be_made_again_and_again(user):
    deep = str(user / "proj" / "src")
    line = "for i in 1 2; do cd ..; done; cat .orcha/key"  # two moves up reach the key
    assert classify_shell(line, cwd=deep) is DESTRUCTIVE
    assert classify_shell("cd ..; cat .orcha/key", cwd=deep) is not DESTRUCTIVE
