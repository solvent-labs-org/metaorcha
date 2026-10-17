"""Story 1.2 — every capability resolves to exactly one scope class (AD-18, AD-17)."""

from __future__ import annotations

import pytest
from superagent.middleware.scope_classes import (
    SYSTEM_TOOL_CLASSES,
    ScopeClass,
    is_valid_capability_id,
    platform_rule,
    resolve_scope_class,
)
from superagent.system_tools.registry import SystemToolRegistry, SystemToolSpec

# ── Platform system tools: the table covers the live registry ────────────────


def _live_system_tool_names(monkeypatch: pytest.MonkeyPatch) -> set[str]:
    """Register every system tool into a scratch registry and return the names.

    The mailer is gated on ``settings.sandbox_mailer`` (read at call time from
    the already-constructed Settings, so an env var set now is too late);
    force it on so the table is checked against the widest set a deployment
    can expose.
    """
    from superagent import config as config_mod
    from superagent.system_tools import registry as registry_mod

    monkeypatch.setattr(config_mod.settings, "sandbox_mailer", True)
    scratch = SystemToolRegistry()
    monkeypatch.setattr(registry_mod, "SYSTEM_TOOL_REGISTRY", scratch)
    registry_mod.register_all_system_tools()
    return set(scratch._tools)


def test_every_registered_system_tool_is_classed(monkeypatch: pytest.MonkeyPatch):
    live = _live_system_tool_names(monkeypatch)
    assert live, "no system tools registered — the enumeration is broken"
    unlisted = live - set(SYSTEM_TOOL_CLASSES)
    assert not unlisted, f"system tools with no scope class: {sorted(unlisted)}"


def test_table_names_no_phantom_system_tool(monkeypatch: pytest.MonkeyPatch):
    live = _live_system_tool_names(monkeypatch)
    phantom = set(SYSTEM_TOOL_CLASSES) - live
    assert not phantom, f"table lists tools the registry does not: {sorted(phantom)}"


def test_no_system_tool_is_destructive():
    # A destructive system tool would pause every chat turn.
    bad = [n for n, c in SYSTEM_TOOL_CLASSES.items() if c is ScopeClass.DESTRUCTIVE]
    assert bad == []


def test_system_tool_table_wins_over_name_pattern():
    # "abandon" is no verb token; the table says write, not destructive.
    assert platform_rule("abandon_checklist") is ScopeClass.WRITE
    assert platform_rule("read_artifact") is ScopeClass.READ


# ── First connections: GitHub, Notion, Linear, Slack ─────────────────────────


@pytest.mark.parametrize(
    "capability",
    [
        "merge_pull_request",
        "delete_file",
        "force_push",
        "push_force",
        "close_issue",
        "archive_issue",
        "notion-delete-page",
        "slack_remove_reaction",
        "closeIssue",
    ],
)
def test_destructive_capabilities(capability: str):
    assert platform_rule(capability) is ScopeClass.DESTRUCTIVE


@pytest.mark.parametrize(
    "capability",
    [
        "create_pull_request",
        "create_issue",
        "notion-create-pages",
        "slack_post_message",
        "create_or_update_file",
        "push_files",
        "update_issue",
        "slack_add_reaction",
        "createIssue",
    ],
)
def test_write_capabilities(capability: str):
    assert platform_rule(capability) is ScopeClass.WRITE


@pytest.mark.parametrize(
    "capability",
    [
        "list_issues",
        "get_pull_request",
        "search_repositories",
        "read_file",
        "notion-search",
        "notion-fetch",
        "slack_list_channels",
        "slack_read_channel",
        "listIssues",
    ],
)
def test_read_capabilities(capability: str):
    assert platform_rule(capability) is ScopeClass.READ


def test_strictest_token_wins_inside_one_name():
    assert platform_rule("create_or_delete_branch") is ScopeClass.DESTRUCTIVE
    assert platform_rule("read_and_update") is ScopeClass.WRITE


# ── Unknown, empty, malformed: destructive, never raise ──────────────────────


@pytest.mark.parametrize(
    "capability", ["", "   ", None, 42, {"name": "x"}, "frobnicate", "x", "did#cap"]
)
def test_unknown_or_malformed_is_destructive(capability):
    assert platform_rule(capability) is ScopeClass.DESTRUCTIVE
    assert resolve_scope_class(capability) is ScopeClass.DESTRUCTIVE


# ── Strictest-of platform rule, manifest, override ───────────────────────────


def test_manifest_may_tighten():
    assert (
        resolve_scope_class("list_issues", manifest_classes={"list_issues": "write"})
        is ScopeClass.WRITE
    )
    assert (
        resolve_scope_class(
            "create_issue", manifest_classes={"create_issue": "destructive"}
        )
        is ScopeClass.DESTRUCTIVE
    )


def test_manifest_cannot_loosen():
    assert (
        resolve_scope_class(
            "merge_pull_request", manifest_classes={"merge_pull_request": "read"}
        )
        is ScopeClass.DESTRUCTIVE
    )
    assert (
        resolve_scope_class("create_issue", manifest_classes={"create_issue": "read"})
        is ScopeClass.WRITE
    )


def test_override_tightens_but_never_loosens():
    assert resolve_scope_class("list_issues", override="destructive") is (
        ScopeClass.DESTRUCTIVE
    )
    assert resolve_scope_class("delete_file", override="read") is (
        ScopeClass.DESTRUCTIVE
    )


def test_unparseable_declarations_are_ignored():
    assert (
        resolve_scope_class(
            "list_issues", manifest_classes={"list_issues": "harmless"}, override=7
        )
        is ScopeClass.READ
    )
    assert resolve_scope_class("list_issues", manifest_classes="nope") is (  # type: ignore[arg-type]
        ScopeClass.READ
    )


def test_unknown_capability_stays_destructive_whatever_the_manifest_says():
    assert (
        resolve_scope_class("frobnicate", manifest_classes={"frobnicate": "read"})
        is ScopeClass.DESTRUCTIVE
    )


# ── AD-17 charset ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "capability", ["search_repos", "notion-fetch", "a.b:c", "X1", "slack_post_message"]
)
def test_valid_capability_ids(capability: str):
    assert is_valid_capability_id(capability)


@pytest.mark.parametrize(
    "capability", ["did#cap", "with space", "", "ünïcode", "a/b", None, "tab\tx"]
)
def test_invalid_capability_ids(capability):
    assert not is_valid_capability_id(capability)


def test_scope_class_ordering_and_labels():
    assert max(ScopeClass.READ, ScopeClass.WRITE) is ScopeClass.WRITE
    assert ScopeClass.parse("Destructive") is ScopeClass.DESTRUCTIVE
    assert ScopeClass.DESTRUCTIVE.label == "destructive"
    assert isinstance(SystemToolSpec, type)  # import kept honest
