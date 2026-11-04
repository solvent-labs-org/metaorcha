from __future__ import annotations

import asyncio
import threading

import anyio
import pytest

from fake_tty import FakeTty
from metabot import approvals
from metabot.approvals import ApprovalRequest, DenyAll, Labelled, Scripted, TtyApprover
from metabot.control import ControlServer, request, socket_path
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


@pytest.mark.parametrize("cancels", [1, 2, 3])
async def test_a_question_taken_back_says_so_before_the_next_one_is_put(tty, cancels):
    approver = TtyApprover(tty.path)
    first = asyncio.create_task(approver.ask(_req(ScopeClass.WRITE, preview="first one")))
    await tty.wait_for("allow?")
    second = asyncio.create_task(approver.ask(_req(ScopeClass.WRITE, preview="second one")))
    await asyncio.sleep(0.05)  # it waits its turn
    for _ in range(cancels):  # its asker gave up on it, and then the run stopped as well
        first.cancel()
        await asyncio.sleep(0.005)
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(first, 5.0)
    await tty.wait_for("second one")
    await asyncio.sleep(approvals.STOP_POLL_S * 3)  # all the first had left to say
    shown = tty.screen()
    # before: the first's line came under the second's prompt, which was still asked
    assert shown.index("cancelled; declined") < shown.index("second one")
    assert "cancelled; declined: claude-code/Bash" in shown  # it names the call
    tty.type("y\n")
    assert await asyncio.wait_for(second, 5.0) is True


async def test_a_stopped_run_s_question_says_so_before_another_run_s_is_put(tty, tmp_path):
    """Two runs share the terminal, as an office's desks do. One stops while its question is
    on it: its server takes the question back, and then its engine, stopped, hangs up."""
    approver = TtyApprover(tty.path)
    servers = []
    for who in ("desk-a", "desk-b"):
        run_dir = tmp_path / who
        run_dir.mkdir()
        sock, asks = socket_path(run_dir), Labelled(approver, who)

        async def on_approve(msg, asks=asks):
            allowed = await asks.ask(_req(ScopeClass.WRITE, preview=msg["call_id"]))
            return {"allow": allowed, "reason": "test"}

        servers.append(
            ControlServer(
                sock,
                on_approve=on_approve,
                on_step=lambda s: None,
                cleanup_dir=sock.parent != run_dir,
            )
        )
    a, b = servers
    for srv in servers:
        await srv.start()
    try:
        ask = {"op": "approve", "cls": "write"}
        engine_a = asyncio.create_task(request(a.path, {**ask, "call_id": "first one"}))
        await tty.wait_for("allow?")
        engine_b = asyncio.create_task(request(b.path, {**ask, "call_id": "second one"}))
        await asyncio.sleep(0.05)  # desk-b's question waits its turn
        a.withdraw()  # desk-a's run is ending
        await asyncio.sleep(0.005)
        engine_a.cancel()  # and its engine, stopped, hangs up
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(engine_a, 5.0)
        await tty.wait_for("second one")
        await asyncio.sleep(approvals.STOP_POLL_S * 3)  # all desk-a had left to say
        shown = tty.screen()
        # before: desk-a's line came under desk-b's prompt, which was still asked
        assert shown.index("desk-a: cancelled; declined") < shown.index("second one")
        tty.type("y\n")
        assert (await asyncio.wait_for(engine_b, 5.0))["allow"] is True
    finally:
        for srv in servers:
            await srv.close()


async def test_a_question_whose_task_group_stops_says_so_before_the_ask_ends(tty):
    """An in-process run's connections hold it in a task group, whose stop is said again on
    every turn of the loop until it ends."""
    approver = TtyApprover(tty.path)
    shown: list[str] = []

    async def asks() -> None:
        try:
            await approver.ask(_req(ScopeClass.WRITE))
        finally:
            shown.append(tty.screen())  # the terminal, as the ask ended

    async with anyio.create_task_group() as group:
        group.start_soon(asks)
        await tty.wait_for("allow?")
        group.cancel_scope.cancel()
    assert "cancelled; declined" in shown[0]  # before: the ask ended first


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
    # a byte of a name that is not UTF-8, as Python reads it, cannot be written out
    assert terminal_safe("caf\udce9") == "caf\\udce9"
    # joiners, accents and emoji are text, not controls
    assert (
        terminal_safe("caf\u00e9 \U0001f469\u200d\U0001f4bb")
        == "caf\u00e9 \U0001f469\u200d\U0001f4bb"
    )
