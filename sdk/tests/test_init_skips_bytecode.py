"""orcha init must survive pip/compileall dropping __pycache__ into the template."""

from __future__ import annotations

from argparse import Namespace
from pathlib import Path

from emerge import cli


def test_init_skips_pycache_and_still_scaffolds(tmp_path: Path, monkeypatch) -> None:
    tpl = tmp_path / "tpl"
    (tpl / "__pycache__").mkdir(parents=True)
    (tpl / "agent.py").write_text("# {{AGENT_NAME}}\n", encoding="utf-8")
    (tpl / "__pycache__" / "agent.cpython-313.pyc").write_bytes(b"\x00\xff\xfe")
    monkeypatch.setattr(cli, "_TEMPLATE_DIR", tpl)

    dest = tmp_path / "my-agent"
    assert cli.cmd_init(Namespace(name="My Agent", dir=str(dest))) == 0
    assert (dest / "agent.py").read_text(encoding="utf-8") == "# My Agent\n"
    assert not (dest / "__pycache__").exists()


def test_init_skips_sibling_pyc(tmp_path: Path, monkeypatch) -> None:
    tpl = tmp_path / "tpl"
    tpl.mkdir()
    (tpl / "agent.py").write_text("# {{AGENT_NAME}}\n", encoding="utf-8")
    (tpl / "agent.pyc").write_bytes(b"\x00\xff\xfe")
    monkeypatch.setattr(cli, "_TEMPLATE_DIR", tpl)

    dest = tmp_path / "my-agent"
    assert cli.cmd_init(Namespace(name="My Agent", dir=str(dest))) == 0
    assert (dest / "agent.py").exists()
    assert not (dest / "agent.pyc").exists()
