"""A real pseudo-terminal for approver tests: the approver opens the follower side by path."""

from __future__ import annotations

import asyncio
import contextlib
import os
import pty


class FakeTty:
    """A real pseudo-terminal: the approver opens the follower side by path."""

    def __init__(self) -> None:
        self.leader, self.follower = pty.openpty()
        self.path = os.ttyname(self.follower)
        self.seen = ""

    def type(self, text: str) -> None:
        os.write(self.leader, text.encode())

    def screen(self) -> str:
        """Everything the approver has shown so far."""
        os.set_blocking(self.leader, False)
        with contextlib.suppress(BlockingIOError):
            self.seen += os.read(self.leader, 65536).decode(errors="replace")
        return self.seen

    async def wait_for(self, text: str, timeout: float = 5.0) -> None:
        deadline = asyncio.get_running_loop().time() + timeout
        while text not in self.screen():
            assert asyncio.get_running_loop().time() < deadline, f"never shown: {text!r}"
            await asyncio.sleep(0.01)

    def close(self) -> None:
        os.close(self.leader)
        os.close(self.follower)
