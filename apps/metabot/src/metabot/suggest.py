"""Suggestions: which role a task suits, and the rule that says so.

A few rules, not a model. Each rule is a short list of whole words, checked
in order, and the first rule with a word in the task picks the role. When
no rule matches, the role is ``build``. A suggestion is a starting point:
``-e`` and ``-m`` still win, and the journal keeps what was suggested next
to what ran. Only what ran is signed, through the ``model`` verdict.

A role with no engine here falls back to ``build``, and says so. A task
that reads as private never falls back to an engine that sends the work
off this machine: it waits for the local model server or the person.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from metabot.approvals import printable

FALLBACK_ROLE = "build"


@dataclass(frozen=True)
class Rule:
    name: str
    pattern: re.Pattern[str]
    role: str


def _words(*alternatives: str) -> re.Pattern[str]:
    """Any of these, as whole words, in any case."""
    return re.compile(r"\b(?:" + "|".join(alternatives) + r")\b", re.IGNORECASE)


RULES: tuple[Rule, ...] = (
    Rule(
        "private",
        _words(r"private(?:ly)?", "offline", r"secrets?", r"confidential(?:ly)?"),
        "private",
    ),
    Rule(
        "review",
        _words(
            r"review(?:s|ed|ing)?",
            r"audit(?:s|ed|ing)?",
            r"critiqu(?:e|es|ed|ing)",
            r"check(?:s|ed|ing)?",
        ),
        "review",
    ),
    Rule(
        "fast",
        _words(
            r"triag(?:e|es|ed|ing)",
            r"summari[sz](?:e|es|ed|ing)",
            r"explain(?:s|ed|ing)?",
            r"list(?:s|ed|ing)?",
            r"find(?:s|ing)?",
            r"what\s+is",
            r"what's",
        ),
        "fast",
    ),
    Rule(
        "build",
        _words(
            r"implement(?:s|ed|ing)?",
            r"build(?:s|ing)?",
            r"refactor(?:s|ed|ing)?",
            r"fix(?:es|ed|ing)?",
            r"writ(?:e|es|ing)",
            r"add(?:s|ed|ing)?",
        ),
        "build",
    ),
)

NOT_HERE = "the task reads as private, and no engine here keeps the work on this machine"
LEAVES = "the private role is set to an engine that sends the work off this machine"
SET_ONE = "set one with: metabot roster set {role} --engine ENGINE"


@dataclass(frozen=True)
class Suggestion:
    role: str
    engine: str | None  # None: nothing here can run it, and ``note`` says why
    model: str | None
    rule: str  # the rule's name, or "default" when none matched
    matched: str | None  # the task's own words that matched the rule
    note: str | None = None

    def record(self) -> dict[str, str | None]:
        """What the journal keeps of a suggestion."""
        return {
            "role": self.role,
            "engine": self.engine,
            "model": self.model,
            "rule": self.rule,
            "matched": self.matched,
        }

    def entry(self) -> dict[str, str]:
        """The suggestion as a roster entry; only when it has an engine."""
        if self.engine is None:
            raise ValueError("this suggestion has no engine")
        return {"engine": self.engine, **({"model": self.model} if self.model else {})}


def match(task: str) -> tuple[Rule | None, str | None]:
    """The first rule with a word in the task, and that word as the task has it."""
    for rule in RULES:
        found = rule.pattern.search(task)
        if found:
            return rule, found.group(0)
    return None, None


def _engine(roster: Mapping[str, Mapping[str, str]], role: str) -> Mapping[str, str] | None:
    entry = roster.get(role)
    return entry if entry and entry.get("engine") else None


def suggest(
    task: str,
    roster: Mapping[str, Mapping[str, str]],
    local: Callable[[Mapping[str, str]], bool] | None = None,
) -> Suggestion:
    """The role a task suits, the roster's engine and model for it, and the rule.

    ``local`` says whether an entry keeps the work on this machine; with it, a
    private task on an engine that does not says so.
    """
    rule, matched = match(task)
    role = rule.role if rule else FALLBACK_ROLE
    name = rule.name if rule else "default"
    entry = _engine(roster, role)
    if entry is not None:
        leaves = role == "private" and local is not None and not local(entry)
        note = LEAVES if leaves else None
        return Suggestion(role, entry["engine"], entry.get("model"), name, matched, note)
    if role == "private":
        note = f"{NOT_HERE}; start the local model server, or {SET_ONE.format(role='private')}"
        return Suggestion(role, None, None, name, matched, note)
    build = _engine(roster, FALLBACK_ROLE)
    if role == FALLBACK_ROLE or build is None:
        nor = f", nor for the {FALLBACK_ROLE} role" if role != FALLBACK_ROLE else ""
        note = f"no engine for the {role} role here{nor}; {SET_ONE.format(role=FALLBACK_ROLE)}"
        return Suggestion(role, None, None, name, matched, note)
    note = f"the {role} role has no engine here, so the {FALLBACK_ROLE} role instead"
    return Suggestion(FALLBACK_ROLE, build["engine"], build.get("model"), name, matched, note)


def suggest_here(task: str) -> Suggestion:
    """A suggestion from the roster as it stands on this machine."""
    from metabot.config import load_config
    from metabot.roster import roster_entries, stays_here

    cfg = load_config()
    return suggest(task, roster_entries(cfg), local=lambda entry: stays_here(entry, cfg))


def reason(s: Suggestion) -> str:
    """Why: the rule, and the task's words that matched it."""
    if s.matched is None:
        return f"default: no rule matched, so the {FALLBACK_ROLE} role"
    return f'{s.rule}: the task says "{printable(s.matched, 40)}"'


# ── the command ─────────────────────────────────────────────────────────────


def cmd_suggest(args: argparse.Namespace) -> int:
    from metabot.commands import EXIT_USAGE, read_prompt

    task = read_prompt(args.task)
    if not task.strip():
        print("metabot: the task is empty", file=sys.stderr)
        return EXIT_USAGE
    s = suggest_here(task)
    rows: list[tuple[str, Any]] = [
        ("role", s.role),
        ("engine", s.engine or "-"),
        ("model", (s.model or "(its own)") if s.engine else "-"),
        ("rule", reason(s)),
    ]
    if s.note:
        rows.append(("note", s.note))
    for label, value in rows:
        print(f"{label:<7} {value}")
    return 0 if s.engine else 1
