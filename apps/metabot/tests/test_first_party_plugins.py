"""MetaBot's own marketplace: the bizopsmap and code-desks plugins.

Their skills only call each tool's own command, so nothing is copied from
either project and nothing drifts. The commands a skill may name are the
ones each tool's help listed when the skills were written.
"""

from __future__ import annotations

import re

import pytest
import yaml

from metabot.plugins import (
    BUILTIN,
    PluginError,
    add_marketplace,
    builtin_marketplace,
    enable,
    enabled_plugin_dirs,
    inspect_plugin,
    install,
    market_plugins,
    marketplaces,
    remove_marketplace,
    tree_sha256,
)
from plugin_kit import make_market, yes

SKILLS = sorted(builtin_marketplace().glob("*/skills/*/SKILL.md"))
COMMANDS = {
    "bizopsmap": {
        "up", "guide", "templates", "init", "detect", "validate", "build", "render", "export",
        "timeline",
    },
    "desk": {"board", "inbox", "deliver", "mail", "send", "verify", "check", "status"},
}  # fmt: skip
NEVER_IN_COPY = re.compile(
    r"\b(claude|anthropic|cursor|codex|openai|gpt|grok|gemini|llama|qwen|trustless|web3)\b",
    re.IGNORECASE,
)


def test_the_metabot_marketplace_is_always_listed_and_each_plugin_installs():
    root = marketplaces()[BUILTIN]
    assert root == builtin_marketplace()
    assert sorted(name for name, _ in market_plugins(root)) == ["bizopsmap", "code-desks"]
    for name in ("bizopsmap", "code-desks"):
        plugin = install(f"{name}@{BUILTIN}", yes)
        facts = inspect_plugin(plugin.root)
        assert (facts.hooks, facts.servers, facts.lsp_servers) == ({}, {}, {})
        assert facts.skills
        enable(plugin.ref)
    assert len(enabled_plugin_dirs()) == 2


def test_the_metabot_name_is_kept(tmp_path):
    market = make_market(tmp_path, "tools")
    with pytest.raises(PluginError, match=BUILTIN):
        add_marketplace(BUILTIN, market)
    with pytest.raises(PluginError, match=BUILTIN):
        remove_marketplace(BUILTIN)


def test_installing_twice_from_the_package_is_one_install():
    first = install(f"bizopsmap@{BUILTIN}", yes)
    again = install(f"bizopsmap@{BUILTIN}", lambda *_: pytest.fail("asked again"))
    assert again.tree == first.tree == tree_sha256(builtin_marketplace() / "bizopsmap")


@pytest.mark.parametrize("skill", SKILLS, ids=lambda p: p.parent.name)
def test_each_skill_has_name_and_description_front_matter(skill):
    head, _, body = skill.read_text(encoding="utf-8").partition("\n---\n")
    front = yaml.safe_load(head.removeprefix("---\n"))
    assert set(front) == {"name", "description"}
    assert front["name"] == skill.parent.name
    assert 40 <= len(front["description"]) <= 1024
    assert body.strip()


@pytest.mark.parametrize("skill", SKILLS, ids=lambda p: p.parent.name)
def test_skills_name_only_real_commands_and_follow_the_copy_rules(skill):
    text = skill.read_text(encoding="utf-8")
    assert not NEVER_IN_COPY.search(text), NEVER_IN_COPY.search(text)
    for tool, known in COMMANDS.items():
        for used in re.findall(rf"`{tool} ([a-z][a-z-]*)", text):
            assert used in known, f"{skill.parent.name}: {tool} {used}"
    assert re.search(r"`(bizopsmap|desk) [a-z]", text), "a skill runs its tool's command"


def test_there_are_three_skills():
    assert sorted(p.parent.name for p in SKILLS) == [
        "bizopsmap-map",
        "bizopsmap-question",
        "code-desks",
    ]
