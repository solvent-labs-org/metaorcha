"""Checks on text that more than one parser here makes."""

from __future__ import annotations


def ascii_digits(text: str) -> bool:
    """Whether ``text`` is all ASCII digits. ``isdigit()`` is also true for ``²``,
    which ``int()`` refuses, so a parser asks this before it calls ``int()``."""
    return text.isascii() and text.isdigit()
