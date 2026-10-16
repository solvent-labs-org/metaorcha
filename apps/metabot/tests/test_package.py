"""The README and the wheel: honest limits, every command, the public copy rules, a clean wheel."""

from __future__ import annotations

import contextlib
import io
import re
import shlex
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

from metabot.cli import _token_flag_given, build_parser, main
from metabot.office import desk_problem, load_office

PKG = Path(__file__).resolve().parents[1]
README = (PKG / "README.md").read_text(encoding="utf-8")
CODE = re.compile(r"```.*?```|`[^`]+`", re.S)  # inline code may wrap a line, as in Markdown
PROSE = CODE.sub(" ", README)


def _help_commands(capsys) -> list[str]:
    with pytest.raises(SystemExit):
        main(["--help"])
    out = capsys.readouterr().out
    block = out.split("positional arguments:")[1].split("options:")[0]
    return re.findall(r"^    ([a-z-]+)\s", block, re.M)


def test_the_readme_covers_every_command(capsys):
    commands = _help_commands(capsys)
    assert len(commands) >= 16, commands
    for command in commands:
        assert f"metabot {command}" in README, command
    for sub in (
        "connect add", "office run", "office trust", "desks pick", "plugin install",
        "plugin marketplace add", "routine save", "receipts verify", "roster set",
    ):  # fmt: skip
        assert f"metabot {sub}" in README, sub


STAND_INS = {"<run-id>": "run-1", "<office-run>": "office-1"}  # the README's placeholders


def _readme_commands(text: str) -> list[str]:
    """Every ``metabot`` line in the code blocks of ``text``, a continued line joined."""
    blocks = re.findall(r"^```[^\n]*\n(.*?)^```", text, re.M | re.S)
    lines = (line.strip() for block in blocks for line in block.replace("\\\n", " ").splitlines())
    return [line for line in lines if line.startswith("metabot ")]


def _refused(text: str) -> list[str]:
    """The ``metabot`` lines in ``text`` that metabot refuses as typed, each with why."""
    refused = []
    for line in _readme_commands(text):
        words = shlex.split(line, comments=True)[1:]
        for placeholder, value in STAND_INS.items():
            words = [word.replace(placeholder, value) for word in words]
        unknown = re.findall(r"<[^<>]+>", " ".join(words))
        if unknown:
            refused.append(f"{line}: no stand-in for {', '.join(unknown)}")
            continue
        if _token_flag_given(words):
            refused.append(f"{line}: a token on the command line")
            continue
        said = io.StringIO()
        try:
            with contextlib.redirect_stderr(said):
                build_parser().parse_args(words)
        except SystemExit:
            refused.append(f"{line}: {said.getvalue().strip().splitlines()[-1]}")
    return refused


def test_every_command_the_readme_shows_parses():
    assert len(_readme_commands(README)) >= 30
    assert _refused(README) == []


def test_a_readme_command_that_does_not_parse_is_named():
    [said] = _refused(README.replace("metabot desks list", "metabot desks lsit"))
    assert said.startswith("metabot desks lsit: ") and "invalid choice" in said


def test_the_readme_states_the_limits():
    limits = README.split("\n## Limits\n", 1)[1].split("\n## ", 1)[0]
    stated = " ".join(limits.split())
    for said in (
        "runs with your privileges",  # a connection's server
        "cannot see into",  # what --allow-write lets run
        "credential",  # a shell read of one never asks
        "signing key",  # and nor does a read of the key
        "own sandbox",  # codex and cursor run their own tools unasked
        "test command",  # a routine declines its project's own checks
        "as text",  # some local models never act
        "exit code",  # exit_zero on a claude run
        "moves the page",  # the browser policy's gap
        "untracked",  # project copies of receipts
        "every port",  # the web UI's cookie
        "use the UI as you",  # and a server that gets the cookie can act with it
    ):
        assert said in stated, said  # in the Limits section, not anywhere
    [unseen] = [b for b in re.split(r"\n- ", limits) if "cannot see into" in " ".join(b.split())]
    assert "`--allow-write`" in unseen  # the limit names the flag that opens it


def test_the_readme_says_what_a_receipt_records_about_each_write():
    section = README.split("\n## Receipts\n", 1)[1].split("\n## ", 1)[0]
    stated = " ".join(section.split())
    assert "who or what let each write through the gate" in stated
    assert "an allow you set before the run" in stated  # the policy's allows are signed too
    assert "own sandbox" in stated  # the writes no verdict speaks for
    assert "says how many under the answer" in stated  # a declined call the answer may claim


def test_the_readme_says_how_a_kit_loads_and_what_it_may_not_bring():
    section = README.split("\n## Offices\n", 1)[1].split("\n## ", 1)[0]
    stated = " ".join(section.split())
    assert "A desk's `kit` loads into its runs." in stated
    assert "never a path" in stated and "must stay inside the project" in stated
    assert "hooks and servers are left out" in stated
    assert "`kits/`" in README  # the files it keeps


def test_the_readme_office_example_loads_as_written(tmp_path, monkeypatch):
    monkeypatch.delenv("DESK_OFFICE", raising=False)
    section = README.split("\n## Offices\n", 1)[1].split("\n## ", 1)[0]
    example = section.split("```yaml\n", 1)[1].split("```", 1)[0]
    (tmp_path / "office.yaml").write_text(example, encoding="utf-8")
    (tmp_path / "roles").mkdir()
    for role in ("build", "review"):
        (tmp_path / "roles" / f"{role}.md").write_text(f"You are {role}.\n", encoding="utf-8")
    office = load_office(tmp_path)
    kinds = {name: (desk.harness, desk.engine) for name, desk in office.desks.items()}
    assert kinds == {
        "build": ("claude", "claude"),
        "second": ("codex", "codex"),
        "local": ("any", "openai"),
        "review": ("codex", "codex"),
    }
    assert office.desks["review"].advises == ("build",)
    assert office.checks() == {"done:tests": "make test"}
    assert all(desk_problem(desk) is None for desk in office.desks.values())
    assert "metabot office run --desks build,local" in section


def test_the_readme_says_the_ui_starts_an_office_run():
    section = README.split("\n## The web UI\n", 1)[1].split("\n## ", 1)[0]
    stated = " ".join(section.split())
    assert "From its office page you start an office run" in stated
    assert "Each desk's asks wait on the run's page, and each names its desk." in stated


def test_the_readme_says_how_sessions_mail_each_other():
    section = README.split("\n## Mail between sessions\n", 1)[1].split("\n## ", 1)[0]
    stated = " ".join(section.split())
    for event in ("UserPromptSubmit", "PostToolUse", "beforeSubmitPrompt", "postToolUse"):
        assert f"`{event}`" in stated
    assert "unless you stopped the turn yourself" in stated
    assert "It approves nothing: a write still asks wherever it runs." in stated


def test_the_readme_keeps_the_public_copy_rules():
    assert "Metaorcha" in README
    assert not re.search(r"(?<![\w./:-])Orcha(?![\w-])", README)  # never a name of its own
    assert not re.search(r"\borcha\b", PROSE, re.I)  # outside code it is Metaorcha
    assert not re.search(r"\b(first|only|nobody|trustless|web3)\b", PROSE, re.I)
    assert "verified safe" not in PROSE.lower()
    assert not re.search(r"\b(tokenomics|airdrop|coming soon)\b", README, re.I)
    # engines by their commands, in code, never as names in prose; no vendor or model names
    assert not re.search(r"\b(claude|codex|cursor|openai)\b", PROSE, re.I)
    vendors = r"\b(anthropic|gpt|grok|gemini|llama|qwen|mistral|deepseek|ollama)\b"
    assert not re.search(vendors, README, re.I)


def test_the_wheel_holds_the_package_and_nothing_else(tmp_path):
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv is not installed")
    built = subprocess.run(
        [uv, "build", "--wheel", "--offline", "--out-dir", str(tmp_path)],
        cwd=PKG,
        capture_output=True,
        text=True,
        timeout=300,
    )
    if built.returncode != 0 and re.search(r"offline|cache|network", built.stderr, re.I):
        pytest.skip(f"the build backend is not cached here: {built.stderr[-300:]}")
    assert built.returncode == 0, built.stderr[-2000:]
    [wheel] = tmp_path.glob("orcha_metabot-*.whl")
    with zipfile.ZipFile(wheel) as zf:
        names = set(zf.namelist())
    assert "metabot/cli.py" in names
    market = PKG / "src" / "metabot" / "marketplace"
    shipped = {p.relative_to(PKG / "src").as_posix() for p in market.rglob("*") if p.is_file()}
    assert shipped and shipped <= names
    assert any(n.endswith(".dist-info/licenses/LICENSE") for n in names), sorted(names)
    stray = [
        n
        for n in names
        if n.startswith("tests/")
        or "/tests/" in n
        or ".venv" in n
        or "dev-pins" in n
        or "__pycache__" in n
        or n.endswith(".pyc")
    ]
    assert stray == []
