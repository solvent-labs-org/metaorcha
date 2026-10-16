from __future__ import annotations

import asyncio
import threading

import pytest

from fake_tty import FakeTty
from metabot import approvals
from metabot.approvals import ApprovalRequest, DenyAll, Scripted, TtyApprover
from metabot.scope_classes import ScopeClass


def _req(cls: ScopeClass, preview: str = "rm -rf build") -> ApprovalRequest:
    return ApprovalRequest(call_id="c1", tool="claude-code/Bash", cls=cls, preview=preview)


@pytest.fixture
def tty():
    t = FakeTty()
    yield t
    t.close()


async def _answer(tty: FakeTty, approver: TtyApprover, req: ApprovalRequest, text: str) -> bool:
    """Ask, wait until the prompt is on screen, then type: the way a person answers."""
    before = tty.screen().count("allow?")
    task = asyncio.create_task(approver.ask(req))
    deadline = asyncio.get_running_loop().time() + 5.0
    while tty.screen().count("allow?") == before:
        assert asyncio.get_running_loop().time() < deadline, "the prompt never appeared"
        await asyncio.sleep(0.01)
    tty.type(text)
    return await asyncio.wait_for(task, 5.0)


async def test_destructive_needs_the_word_yes(tty):
    approver = TtyApprover(tty.path)
    assert await _answer(tty, approver, _req(ScopeClass.DESTRUCTIVE), "y\n") is False
    assert await _answer(tty, approver, _req(ScopeClass.DESTRUCTIVE), "yes\n") is True


async def test_write_accepts_y(tty):
    approver = TtyApprover(tty.path)
    assert await _answer(tty, approver, _req(ScopeClass.WRITE), "y\n") is True
    assert await _answer(tty, approver, _req(ScopeClass.WRITE), "\n") is False


async def test_prompt_escapes_control_characters(tty):
    approver = TtyApprover(tty.path)
    req = _req(ScopeClass.WRITE, preview="ok\x1b[2J\x1b[Hfake prompt")
    await _answer(tty, approver, req, "n\n")
    shown = tty.screen()
    assert "\x1b[2J" not in shown
    assert "\\x1b[2J" in shown


async def test_type_ahead_never_answers(tty):
    approver = TtyApprover(tty.path)
    tty.type("yes\n")  # typed while the run was busy, before any prompt
    assert await _answer(tty, approver, _req(ScopeClass.DESTRUCTIVE), "no\n") is False


async def test_cancel_lets_go_of_the_terminal(tty):
    approver = TtyApprover(tty.path)
    returned = threading.Event()
    original = approver._ask_sync

    def tracked(*args):
        try:
            return original(*args)
        finally:
            returned.set()

    approver._ask_sync = tracked
    task = asyncio.create_task(approver.ask(_req(ScopeClass.DESTRUCTIVE)))
    await tty.wait_for("allow?")
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # The worker thread must not stay blocked on the terminal (that hangs exit).
    assert await asyncio.to_thread(returned.wait, 2.0)
    await tty.wait_for("cancelled; declined")


async def test_deny_all_and_scripted():
    assert await DenyAll().ask(_req(ScopeClass.WRITE)) is False
    scripted = Scripted([True, False])
    assert await scripted.ask(_req(ScopeClass.WRITE)) is True
    assert await scripted.ask(_req(ScopeClass.WRITE)) is False
    assert await scripted.ask(_req(ScopeClass.WRITE)) is False
    assert scripted.asked == 3


def test_pick_approver_without_tty(monkeypatch):
    def no_tty(*_a, **_k):
        raise OSError("no controlling terminal")

    monkeypatch.setattr(approvals, "_open_tty", no_tty)
    assert isinstance(approvals.pick_approver(), DenyAll)


def test_terminal_safe_keeps_text_and_escapes_what_moves_the_cursor():
    from metabot.approvals import terminal_safe

    # windows line ends become lines; a lone carriage return would overwrite one
    assert terminal_safe("a\r\nb\rc") == "a\nb\\rc"
    assert terminal_safe("tab\there\nnext") == "tab\there\nnext"
    assert terminal_safe("\x1b[2J\x07\x00\x7f\x9b") == "\\x1b[2J\\x07\\x00\\x7f\\x9b"
    assert terminal_safe("\u202eabc\u2066d\u200f") == "\\u202eabc\\u2066d\\u200f"
    # joiners, accents and emoji are text, not controls
    assert (
        terminal_safe("caf\u00e9 \U0001f469\u200d\U0001f4bb")
        == "caf\u00e9 \U0001f469\u200d\U0001f4bb"
    )
