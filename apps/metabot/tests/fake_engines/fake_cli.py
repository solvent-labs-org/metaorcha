"""A stand-in engine CLI for tests. Every behaviour is set by environment:

FAKE_FIXTURE      a JSONL file replayed to stdout, one line at a time
FAKE_EXIT_AFTER   stop replaying after this many lines
FAKE_EXIT         the exit code (default 0)
FAKE_STDERR       text written to stderr before exiting
FAKE_SLEEP        seconds to sleep after replaying (to test cancellation)
FAKE_ARGV_OUT     write {"argv", "pid", "env_keys", "claude_config_dir"} here
FAKE_READ_STDIN   "1": read stdin to EOF first (hangs unless stdin is closed)
FAKE_HOLDER_OUT   spawn a detached `sleep 30` that inherits stdout and stderr
                  (it outlives this process), and write its pid here
FAKE_CONTROL      a control socket path, or "from-hook" to take it from the gate
                  hook in --settings: send FAKE_CONTROL_MSG (JSON) there after
                  the replay, and print the reply as a JSON line
"""

from __future__ import annotations

import json
import os
import sys
import time


def _control_from_hook(argv: list[str]) -> str:
    """The control socket named in the gate hook command of a --settings argument."""
    import shlex

    settings = json.loads(argv[argv.index("--settings") + 1])
    command = settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    words = shlex.split(command)
    return words[words.index("--control") + 1]


def main() -> int:
    out = os.environ.get("FAKE_ARGV_OUT")
    if out:
        with open(out, "w", encoding="utf-8") as fh:
            json.dump(
                {
                    "argv": sys.argv[1:],
                    "pid": os.getpid(),
                    "env_keys": sorted(os.environ),
                    "claude_config_dir": os.environ.get("CLAUDE_CONFIG_DIR"),
                },
                fh,
            )
    if os.environ.get("FAKE_READ_STDIN") == "1":
        sys.stdin.read()
    fixture = os.environ.get("FAKE_FIXTURE")
    limit = int(os.environ.get("FAKE_EXIT_AFTER", "0") or 0)
    if fixture:
        with open(fixture, encoding="utf-8") as fh:
            for n, line in enumerate(fh, start=1):
                sys.stdout.write(line if line.endswith("\n") else line + "\n")
                sys.stdout.flush()
                if limit and n >= limit:
                    break
    control = os.environ.get("FAKE_CONTROL")
    if control == "from-hook":
        control = _control_from_hook(sys.argv[1:])
    if control:
        from metabot import control as ctl

        reply = ctl.request_sync(control, json.loads(os.environ["FAKE_CONTROL_MSG"]))
        sys.stdout.write(json.dumps({"type": "control_reply", "reply": reply}) + "\n")
        sys.stdout.flush()
    holder_out = os.environ.get("FAKE_HOLDER_OUT")
    if holder_out:
        import subprocess

        holder = subprocess.Popen(["sleep", "30"], start_new_session=True)
        with open(holder_out, "w", encoding="utf-8") as fh:
            fh.write(str(holder.pid))
    stderr = os.environ.get("FAKE_STDERR")
    if stderr:
        sys.stderr.write(stderr)
        sys.stderr.flush()
    sleep = float(os.environ.get("FAKE_SLEEP", "0") or 0)
    if sleep:
        time.sleep(sleep)
    return int(os.environ.get("FAKE_EXIT", "0") or 0)


if __name__ == "__main__":
    sys.exit(main())
