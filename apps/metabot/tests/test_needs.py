"""Doctor names the command a bundled plugin's skills call, when that plugin is on."""

from __future__ import annotations

from metabot.needs import doctor_lines
from metabot.plugins import InstalledPlugin


def _plugin(name: str, *, enabled: bool = True) -> InstalledPlugin:
    return InstalledPlugin(
        tree="a" * 64,
        name=name,
        market="local",
        version="1",
        source=".",
        installed_at="t",
        enabled=enabled,
    )


def _only(monkeypatch, plugins: list[InstalledPlugin]) -> None:
    monkeypatch.setattr("metabot.needs.enabled_plugins", lambda: plugins)


def test_a_desk_on_path_is_named(tmp_path, monkeypatch):
    desk = tmp_path / "desk"
    desk.write_text("#!/bin/sh\n", encoding="utf-8")
    desk.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    _only(monkeypatch, [_plugin("code-desks")])
    assert doctor_lines() == [f"plugin   code-desks  needs desk: {desk}"]


def test_a_missing_desk_names_where_it_comes_from(monkeypatch):
    monkeypatch.setenv("PATH", "/no/such/bin")
    _only(monkeypatch, [_plugin("code-desks"), _plugin("bizopsmap")])
    lines = doctor_lines()
    assert lines == [
        "plugin   code-desks  needs desk: missing: it comes with Code Desks",
        "plugin   bizopsmap  needs bizopsmap: missing: it comes with bizopsmap",
    ]
    for line in lines:
        assert "http" not in line and "install" not in line


def test_a_disabled_plugin_is_not_listed(monkeypatch):
    monkeypatch.setenv("PATH", "/no/such/bin")
    _only(monkeypatch, [_plugin("code-desks", enabled=False), _plugin("bizopsmap")])
    lines = doctor_lines()
    assert lines == ["plugin   bizopsmap  needs bizopsmap: missing: it comes with bizopsmap"]
    assert all("code-desks" not in line for line in lines)


def test_a_plugin_outside_the_first_party_list_is_not_listed(monkeypatch):
    monkeypatch.setenv("PATH", "/no/such/bin")
    _only(monkeypatch, [_plugin("tools"), _plugin("code-desks", enabled=False)])
    assert doctor_lines() == []
