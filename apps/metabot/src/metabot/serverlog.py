"""Where a connection server's stderr goes: through a pipe, with its tokens replaced.

A server is started with the write end of a pipe as its stderr. A thread
copies what arrives to the run's log, and every connection token is replaced
by its marker before a byte is written, so a server that prints its own token
never puts it on disk. MetaBot's own notes about a server go through the same
pipe, so the log has one writer.

A line is written once it ends. A line that never ends is written in pieces,
holding back enough at the end that a token cannot be cut in two.
"""

from __future__ import annotations

import codecs
import contextlib
import os
import threading
from collections.abc import Mapping
from typing import TextIO

from emerge.preimage import redact_credentials

CHUNK = 65536  # bytes per read, and the longest line held back before it is cut
JOIN_S = 2.0  # how long close() waits for a server's last words


class RedactedLog:
    def __init__(self, out: TextIO, secrets: Mapping[str, str]) -> None:
        self.out = out
        self.secrets = {var: token for var, token in secrets.items() if token}
        self._hold = max((len(token) for token in self.secrets.values()), default=1) - 1
        read_fd, write_fd = os.pipe()
        self._read_fd = read_fd
        # What a server gets as its stderr (it needs a real file descriptor).
        self.writer = os.fdopen(write_fd, "w", encoding="utf-8", errors="replace")
        self._thread = threading.Thread(target=self._copy, name="metabot-server-log", daemon=True)
        self._thread.start()

    def note(self, text: str) -> None:
        """MetaBot's own line about a server, redacted like the server's."""
        with contextlib.suppress(OSError, ValueError):
            self.writer.write(text.rstrip("\n") + "\n")
            self.writer.flush()

    def close(self) -> None:
        """Stop taking input and wait, briefly, for what is already in the pipe."""
        with contextlib.suppress(OSError, ValueError):
            self.writer.close()
        # The copy ends when every server's end of the pipe is closed too. A
        # process that keeps it open is left to the thread, which ends with it.
        self._thread.join(JOIN_S)

    def _copy(self) -> None:
        decode = codecs.getincrementaldecoder("utf-8")("replace").decode
        pending = ""
        try:
            while chunk := os.read(self._read_fd, CHUNK):
                pending = self._emit(pending + decode(chunk), final=False)
            self._emit(pending + decode(b"", final=True), final=True)
        except (OSError, ValueError):  # the log was closed under the copy
            pass
        finally:
            os.close(self._read_fd)

    def _emit(self, text: str, *, final: bool) -> str:
        """Write what is safe to write now; return what must wait for more."""
        if self.secrets:
            text = redact_credentials(text, self.secrets)
        if final:
            cut = len(text)
        else:
            cut = text.rfind("\n") + 1
            if len(text) - cut > CHUNK:  # a long line: keep only where a token could start
                cut = max(cut, len(text) - self._hold)
        if cut:
            self.out.write(text[:cut])
            self.out.flush()
        return text[cut:]
