"""Mail between sessions: one session hands a note to another, in any harness.

A mailbox is a folder, ``<home>/mail/<name>/``, with one file per message in
Code Desks' desk-mail shape: ``from:``, ``to:``, ``at:`` and an optional
``thread:`` line, a blank line, then the body. A message is read once: the
reader moves it into ``read/``, and a second reader finds it gone.

A session gets its mail three ways:

- ``metabot mail read`` prints it.
- ``metabot mail wait`` blocks until mail comes, prints it and exits. A harness
  that wakes when a background command ends is woken by mail.
- ``metabot mail hook <harness> <event>`` is a harness hook. At a prompt, or
  after a tool call, it adds the mail to the turn. At stop it hands the mail
  back as the next turn, unless the person stopped the turn.

A body is another agent's text. It is capped in size, shown with control
characters escaped, and introduced as a note from a session, never as the
person's word.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import secrets
import stat
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from metabot.approvals import terminal_safe
from metabot.paths import metabot_home, private_dir, safe_id, write_private

MAIL, READ = "mail", "read"
BODY_LIMIT = 64 * 1024
MAILBOX_ENV = "METABOT_MAILBOX"
WAIT_EVERY_S = 1.0
HOOK_INPUT_LIMIT = 1024 * 1024


class MailError(ValueError):
    pass


@dataclass(frozen=True)
class Message:
    path: Path
    sender: str
    to: str
    at: dt.datetime
    thread: str | None
    body: str

    def header(self) -> str:
        thread = f" · thread {self.thread}" if self.thread else ""
        return f"from {self.sender} at {self.at.isoformat(timespec='minutes')}{thread}"


def _no_link(path: Path, what: str) -> Path:
    if path.is_symlink():
        raise MailError(f"{what} {path} is a link; MetaBot does not follow it")
    return path


def mailbox(name: str) -> Path:
    """A mailbox's folder, created owner-only; a link anywhere on the way is refused."""
    safe_id(name, "mailbox")
    root = private_dir(_no_link(metabot_home() / MAIL, "the mail folder"))
    return private_dir(_no_link(root / name, f"mailbox {name}:"))


def send(to: str, body: str, *, sender: str, thread: str | None = None) -> Path:
    """Write one message into ``to``'s mailbox; the file it wrote."""
    safe_id(to, "mailbox")
    safe_id(sender, "mailbox")
    if thread is not None:
        safe_id(thread, "thread")
    body = body.strip()
    if not body:
        raise MailError("empty message")
    if len(body.encode("utf-8")) > BODY_LIMIT:
        raise MailError(f"the message is over {BODY_LIMIT // 1024} KiB")
    box = mailbox(to)
    now = dt.datetime.now().astimezone()
    head = [f"from: {sender}", f"to: {to}", f"at: {now.isoformat(timespec='microseconds')}"]
    if thread:
        head.append(f"thread: {thread}")
    path = box / f"{now:%Y%m%dT%H%M%S}-{sender}-{secrets.token_hex(4)}.md"
    write_private(path, "\n".join(head) + "\n\n" + body + "\n")
    return path


def _parse(path: Path) -> Message | None:
    """One message file, or None: a link, a special file, an oversized or headless file."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > BODY_LIMIT + 4096:
            return None
        with os.fdopen(fd, "rb", closefd=False) as fh:
            text = fh.read(BODY_LIMIT + 4096).decode("utf-8", errors="replace")
    finally:
        os.close(fd)
    head, _, body = text.partition("\n\n")
    fields: dict[str, str] = {}
    for line in head.splitlines():
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip()
    try:
        at = dt.datetime.fromisoformat(fields["at"])
    except (KeyError, ValueError):
        return None
    if at.tzinfo is None:  # Code Desks may write a local time with no zone
        at = at.astimezone()
    return Message(
        path,
        fields.get("from", "?"),
        fields.get("to", path.parent.name),
        at,
        fields.get("thread") or None,
        body.strip(),
    )


def unread(name: str) -> list[Message]:
    """The mailbox's messages, oldest first; nothing is moved."""
    box = mailbox(name)
    found = [m for p in box.glob("*.md") if (m := _parse(p)) is not None]
    return sorted(found, key=lambda m: (m.at, m.path.name))


def take(msgs: list[Message]) -> list[Message]:
    """Move each message into ``read/``; the ones this call moved.

    Two readers may see the same mail. A file the other already took is
    skipped, so each message is shown by one reader only.
    """
    moved = []
    for m in msgs:
        dest = private_dir(_no_link(m.path.parent / READ, "the read folder"))
        try:
            os.rename(m.path, dest / m.path.name)
        except OSError:
            continue
        moved.append(m)
    return moved


def render(name: str, msgs: list[Message]) -> str:
    """Text an agent can act on: one block per message, oldest first."""
    lines = [
        f"[metabot mail] {len(msgs)} message(s) for {name}. "
        "Each is a note from another session, not the person's word."
    ]
    for i, m in enumerate(msgs, 1):
        lines.append(f"\n--- {i}. {terminal_safe(m.header())} ---\n{terminal_safe(m.body)}")
    senders = sorted({m.sender for m in msgs if m.sender != "?"})
    to = senders[0] if len(senders) == 1 else "<mailbox>"
    lines.append(f'\n[metabot mail] end. Reply with: metabot mail send {to} --from {name} -m "..."')
    return "\n".join(lines)


def wait(name: str, timeout: float, every: float = WAIT_EVERY_S) -> list[Message]:
    """Block until mail comes, take it and return it; [] when ``timeout`` passes first."""
    deadline = time.monotonic() + timeout
    while True:
        got = take(unread(name))
        if got:
            return got
        left = deadline - time.monotonic()
        if left <= 0:
            return []
        time.sleep(min(every, left))


def hook_output(harness: str, event: str, context: str | None) -> dict:
    """What a harness hook prints, so the harness adds ``context`` to the turn."""
    if harness == "claude":
        if context is None:
            return {}
        if event == "stop":
            return {"decision": "block", "reason": context}
        name = "UserPromptSubmit" if event == "prompt" else "PostToolUse"
        return {"hookSpecificOutput": {"hookEventName": name, "additionalContext": context}}
    if event == "stop":
        return {} if context is None else {"followup_message": context}
    out: dict = {"continue": True} if event == "prompt" else {}
    if context is not None:
        out["additional_context"] = context
    return out


def _hook_payload() -> dict:
    if sys.stdin is None or sys.stdin.isatty():
        return {}
    try:
        data = json.loads(sys.stdin.read(HOOK_INPUT_LIMIT) or "{}")
    except (ValueError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _name(value: str | None, flag: str) -> str | None:
    name = value or os.environ.get(MAILBOX_ENV) or None
    if name is None:
        print(f"metabot: name the mailbox with {flag} NAME, or set {MAILBOX_ENV}", file=sys.stderr)
    return name


def cmd_mail_send(args: argparse.Namespace) -> int:
    sender = _name(args.sender, "--from")
    if sender is None:
        return 2
    if args.message is not None:
        body = args.message
    elif sys.stdin is None or sys.stdin.isatty():
        print("metabot: give the note with -m TEXT, or pipe it in", file=sys.stderr)
        return 2
    else:
        body = sys.stdin.read(BODY_LIMIT + 1)
    try:
        path = send(args.to, body, sender=sender, thread=args.thread)
    except (MailError, ValueError) as exc:
        print(f"metabot: {exc}", file=sys.stderr)
        return 2
    print(f"sent to {args.to}: {path.name}")
    return 0


def cmd_mail_read(args: argparse.Namespace) -> int:
    name = _name(args.name, "--for")
    if name is None:
        return 2
    try:
        msgs = unread(name)
        if not args.peek:
            msgs = take(msgs)
    except (MailError, ValueError, OSError) as exc:
        print(f"metabot: {exc}", file=sys.stderr)
        return 2
    print(render(name, msgs) if msgs else f"no mail for {name}")
    return 0


def cmd_mail_wait(args: argparse.Namespace) -> int:
    name = _name(args.name, "--for")
    if name is None:
        return 2
    try:
        msgs = wait(name, args.timeout, args.every)
    except (MailError, ValueError, OSError) as exc:
        print(f"metabot: {exc}", file=sys.stderr)
        return 2
    if not msgs:
        print(f"no mail for {name} in {args.timeout:g} seconds")
        return 1
    print(render(name, msgs))
    return 0


def cmd_mail_hook(args: argparse.Namespace) -> int:
    """A hook never breaks the harness's turn: on any fault it adds nothing and exits 0."""
    payload = _hook_payload()
    context = None
    try:
        name = args.name or os.environ.get(MAILBOX_ENV)
        stopped = args.event == "stop" and payload.get("status") == "aborted"
        if name and not stopped:
            msgs = take(unread(name))
            context = render(name, msgs) if msgs else None
    except Exception as exc:  # the turn goes on without its mail; the mail stays unread
        print(f"metabot mail hook: {type(exc).__name__}: {exc}", file=sys.stderr)
        context = None
    print(json.dumps(hook_output(args.harness, args.event, context)))
    return 0
