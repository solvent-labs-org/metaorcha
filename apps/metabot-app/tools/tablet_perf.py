#!/usr/bin/env python3
"""The perf half of tools/tablet-e2e.sh: measurements for the perf_scroll_type and
sheet_taps journeys, standard library only. adb does the talking; this file parses.

The frame source is `dumpsys gfxinfo <pkg> framestats` (the new schema: Flags,
FrameTimelineVsyncId, IntendedVsync, Vsync, InputEventId, HandleInputStart, ...,
FrameCompleted, ..., DisplayPresentTime — no OldestInputEvent on this generation).
Two budgets, both record-only for the first three tablet runs (plan T2):

  scroll  median fps over a scripted fling session, from Vsync deltas
  typing  input-handled -> present per key, all on the frame clock:
          DisplayPresentTime (FrameCompleted when the panel does not report one)
          minus the frame's HandleInputStart. One clock, no conversion — Android's
          /proc/uptime counts suspend time and the frame clock does not (a day of
          standby reads as a -75000 s "latency"), so the /proc/uptime injection
          windows bracketing each `input keyevent` are kept in keys.json as
          evidence but never enter the math. The number understates true
          keydown->present by the OS->app queue time and says so.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

# header names we read, resolved against whatever order the device prints
WANT = ("Vsync", "InputEventId", "HandleInputStart", "FrameCompleted", "DisplayPresentTime")


def parse_profiledata(text):
    """The rows of the last PROFILEDATA block as (column-name -> int) dicts.
    A row that stops early keeps what it had; anything non-numeric is skipped."""
    blocks = re.findall(r"---PROFILEDATA---\n(.*?)(?:---PROFILEDATA---|\Z)", text, re.S)
    rows = []
    for block in blocks:
        lines = [l for l in block.strip().splitlines() if l.strip()]
        if not lines:
            continue
        header = [h.strip() for h in lines[0].split(",") if h.strip()]
        idx = {name: header.index(name) for name in WANT if name in header}
        if len(idx) != len(WANT):
            continue
        for line in lines[1:]:
            fields = [f.strip() for f in line.split(",")]
            try:
                row = {name: int(fields[i]) for name, i in idx.items() if i < len(fields) and fields[i]}
            except ValueError:
                continue
            if len(row) == len(WANT):
                rows.append(row)
    seen, out = set(), []
    for row in rows:                       # the dump can repeat a block; Vsync ids dedupe
        key = row["Vsync"]
        if key not in seen:
            seen.add(key)
            out.append(row)
    out.sort(key=lambda r: r["Vsync"])
    return out


def median(xs):
    xs = sorted(xs)
    if not xs:
        return None
    mid = len(xs) // 2
    return xs[mid] if len(xs) % 2 else (xs[mid - 1] + xs[mid]) / 2


def percentile(xs, p):
    """Nearest-rank: the p-th percentile is the smallest value at least p% of the
    samples sit at or under (ceil(p/100 * n), 1-based)."""
    xs = sorted(xs)
    if not xs:
        return None
    import math
    k = max(0, min(len(xs) - 1, math.ceil(p / 100 * len(xs)) - 1))
    return xs[k]


def scroll_fps(rows, since_vsync=0):
    """Median fps over the frames newer than the watermark: 1e9 / median Vsync delta."""
    vsyncs = [r["Vsync"] for r in rows if r["Vsync"] > since_vsync]
    deltas = [b - a for a, b in zip(vsyncs, vsyncs[1:]) if b > a]
    if not deltas:
        return None, 0
    med = median(deltas)
    return 1e9 / med, len(deltas)


def present_ns(row):
    return row["DisplayPresentTime"] or row["FrameCompleted"]


def key_latencies(rows, keys, since_vsync=0):
    """One latency per injected key, matched in order to frames that report an input
    event (InputEventId != 0) and a HandleInputStart: present minus HandleInputStart,
    all on the frame clock. `keys` is the list of (lo_ns, hi_ns) /proc/uptime
    injection windows — it sets how many frames we expect and is recorded beside the
    result, but never enters the math (the two clocks disagree by suspend time)."""
    frames = [r for r in rows if r["Vsync"] > since_vsync
              and r["InputEventId"] != 0 and r["HandleInputStart"] != 0]
    out = []
    for (lo, hi), row in zip(keys, frames):
        out.append({"ms": (present_ns(row) - row["HandleInputStart"]) / 1e6,
                    "window_ms": (hi - lo) / 1e6})
    return out, len(frames)


# --- adb + device verbs ---------------------------------------------------------

class Adb:
    def __init__(self, adb, serial):
        self.adb, self.serial = adb, serial

    def __call__(self, *args, timeout=30):
        return subprocess.run([self.adb, "-s", self.serial, *args],
                              capture_output=True, text=True, timeout=timeout).stdout

    def shell(self, cmd, timeout=30):
        return self("shell", cmd, timeout=timeout)

    def frames(self, pkg):
        return parse_profiledata(self.shell(f"dumpsys gfxinfo {pkg} framestats", timeout=60))

    def watermark(self, pkg):
        rows = self.frames(pkg)
        return rows[-1]["Vsync"] if rows else 0

    def uptime(self):
        out = self.shell('cut -d" " -f1 /proc/uptime').strip()
        return float(out)

    def key(self, keycode):
        """One injected key with its device-side window: /proc/uptime brackets the call.
        Returns (lo_ns, hi_ns) on the device monotonic clock."""
        out = self.shell(
            'u1=$(cut -d" " -f1 /proc/uptime); input keyevent %s; u2=$(cut -d" " -f1 /proc/uptime); echo "$u1 $u2"' % keycode)
        lo, hi = (float(x) for x in out.split())
        return int(lo * 1e9), int(hi * 1e9)

    def swipe(self, x1, y1, x2, y2, ms):
        self.shell(f"input swipe {x1} {y1} {x2} {y2} {ms}")

    def tap(self, x, y):
        self.shell(f"input tap {x} {y}")

    def webview_bounds(self, pkg):
        """The WebView node's [x1,y1][x2,y2] from a uiautomator dump, or None."""
        self.shell("uiautomator dump /sdcard/tablet-perf.xml")
        xml = self.shell("cat /sdcard/tablet-perf.xml")
        self.shell("rm /sdcard/tablet-perf.xml")
        m = re.search(r'class="android\.webkit\.WebView"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', xml)
        return tuple(int(g) for g in m.groups()) if m else None

    def screen_size(self, pkg):
        b = self.webview_bounds(pkg)
        if b:
            return b[2] - b[0], b[3] - b[1]
        out = self.shell("wm size")
        m = re.search(r"(\d+)x(\d+)", out)
        return (int(m.group(1)), int(m.group(2))) if m else (1280, 800)


# --- the server API (seed) --------------------------------------------------------

def api(port, token, method, path, body=None, timeout=10):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}?t={token}", data=data,
                                 method=method, headers={"Content-Type": "application/json",
                                                         "X-MetaChat": "1"})   # the CSRF marker, as the CLI sends
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {}


def tmux(*args, timeout=10):
    """One tmux call on the server's own socket (config tmux_socket, "metabot" when unset;
    MB_TMUX_SOCKET overrides, "" names the user's default server). Only ever aimed at a
    desk this script just spawned — never at another server's session."""
    sock = os.environ.get("MB_TMUX_SOCKET", "metabot")
    base = ["tmux"] if sock == "" else ["tmux", "-L", sock]
    r = subprocess.run([*base, *args], capture_output=True, text=True, timeout=timeout)
    return r.returncode, (r.stderr or r.stdout).strip()


def seed(port, token, cwd, n, label="perf"):
    """A shell desk (spawned by the API) with n messages in its lane, sent from a
    "seeder" so they land as lane cards (`via: lane`) — a desk with no CLI still
    holds its lane. Returns (desk, sent, milliseconds)."""
    start = time.monotonic()
    status, j = api(port, token, "POST", "/api/spawn", {"kind": "shell", "cwd": cwd})
    if status != 200:
        raise SystemExit(f"seed: spawn refused: {status} {j}")
    desk = j.get("name") or j.get("desk") or ""
    sent = 0
    for i in range(n):
        status, j = api(port, token, "POST", "/api/send",
                        {"text": f"{label} message {i + 1} of {n} — the scroll lane filler, a little longer so the bubble wraps",
                         "target": desk, "from": "seeder", "kind": "message"})
        if status != 200 or not j.get("ok"):
            break
        sent += 1
    return desk, sent, int((time.monotonic() - start) * 1000)


def needs_you(port, token, desk):
    """(needs_you, ask, cls) as the server's pane classifier reads the desk."""
    status, body = api(port, token, "GET", "/api/desks")
    if status != 200:
        return False, None, None
    for row in body.get("desks") or []:
        if row.get("name") == desk:
            return bool(row.get("needs_you")), row.get("ask"), row.get("cls")
    return False, None, None


def tail(desk):
    """The desk's visible pane plus scrollback, via its tmux session."""
    rc, out = tmux("capture-pane", "-p", "-J", "-S", "-", "-t", f"=fc-{desk}:")
    if rc != 0:
        raise SystemExit(f"tail: tmux refused fc-{desk}: {out.strip()}")
    return out


def read_token(state):
    return (Path(state) / "token").read_text().strip()


def main(argv):
    if len(argv) < 2:
        raise SystemExit("usage: tablet_perf.py seed|spawn|send|tail|watermark|frames|fps|keys|type-latency|bounds ...")
    cmd, rest = argv[1], argv[2:]
    if cmd == "seed":                      # seed PORT STATEDIR CWD N
        desk, sent, ms = seed(int(rest[0]), read_token(rest[1]), rest[2], int(rest[3]))
        print(json.dumps({"desk": desk, "sent": sent, "ms": ms}))
    elif cmd == "needs-you":               # needs-you PORT STATEDIR DESK — "True line write" when classified
        print(*needs_you(int(rest[0]), read_token(rest[1]), rest[2]))
    elif cmd == "spawn":                   # spawn PORT STATEDIR CWD — a fresh shell desk, its name on stdout
        start = time.monotonic()
        status, j = api(int(rest[0]), read_token(rest[1]), "POST", "/api/spawn",
                        {"kind": "shell", "cwd": rest[2]})
        if status != 200:
            raise SystemExit(f"spawn refused: {status} {j}")
        print(json.dumps({"desk": j.get("name") or j.get("desk") or "", "ms": int((time.monotonic() - start) * 1000)}))
    elif cmd == "send":                    # send DESK TEXT — typed into the desk's tmux session, Enter ends the line
        rc, out = tmux("send-keys", "-t", f"=fc-{rest[0]}:", rest[1], "Enter")
        if rc != 0:
            raise SystemExit(f"send: tmux refused fc-{rest[0]}: {out.strip()}")
        print("ok")
    elif cmd == "tail":                    # tail DESK — the pane and its scrollback
        print(tail(rest[0]))
    elif cmd == "watermark":               # watermark ADB SERIAL PKG
        print(Adb(rest[0], rest[1]).watermark(rest[2]))
    elif cmd == "frames":                  # frames ADB SERIAL PKG OUTFILE — the raw dump, kept as evidence
        raw = Adb(rest[0], rest[1]).shell(f"dumpsys gfxinfo {rest[2]} framestats", timeout=60)
        Path(rest[3]).write_text(raw)
        print(len(parse_profiledata(raw)))
    elif cmd == "fps":                     # fps RAWFILE WATERMARK
        rows = parse_profiledata(Path(rest[0]).read_text())
        fps, n = scroll_fps(rows, since_vsync=int(rest[1]))
        print(json.dumps({"fps_median": fps, "deltas": n}))
    elif cmd == "keys":                    # keys ADB SERIAL N INTERVAL_MS — inject, print the windows
        a, n, gap = Adb(rest[0], rest[1]), int(rest[2]), float(rest[3]) / 1000
        windows = []
        for i in range(n):
            windows.append(a.key("KEYCODE_A" if i % 2 == 0 else "KEYCODE_B"))
            time.sleep(gap)
        print(json.dumps({"windows": windows}))
    elif cmd == "type-latency":            # type-latency RAWFILE KEYSJSON WATERMARK
        rows = parse_profiledata(Path(rest[0]).read_text())
        keys = [tuple(w) for w in json.loads(Path(rest[1]).read_text())["windows"]]
        lat, matched = key_latencies(rows, keys, since_vsync=int(rest[2]))
        ms = [l["ms"] for l in lat]
        print(json.dumps({"p50_ms": median(ms), "p95_ms": percentile(ms, 95),
                          "keys": len(keys), "matched_frames": matched,
                          "window_ms_max": max((l["window_ms"] for l in lat), default=None)}))
    elif cmd == "bounds":                  # bounds ADB SERIAL PKG — "x1 y1 x2 y2 density"
        a = Adb(rest[0], rest[1])
        b = a.webview_bounds(rest[2]) or (0, 0, *a.screen_size(rest[2]))
        m = re.search(r"(\d+)", a.shell("wm density"))
        print(*b, m.group(1) if m else 160)
    else:
        raise SystemExit(f"unknown command {cmd!r}")


if __name__ == "__main__":
    main(sys.argv)
