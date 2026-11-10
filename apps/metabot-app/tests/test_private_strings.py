"""B4: nothing in the shipped tree names this developer's machine, people, network or
private tools. The app, its tests and its docs all go public, so all are walked."""
import hashlib
import re
import unittest
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
SELF = Path(__file__).resolve()
SKIP_DIRS = {"__pycache__", "target", "node_modules", "dist", "wheels", ".git"}
SKIP_SUFFIX = {".woff2", ".woff", ".ttf", ".png", ".jpg", ".ico", ".jar", ".apk", ".gz"}

# Built from parts so this file does not match itself if it is ever walked.
PATTERNS = {
    "a person's name": re.compile("az" + "ank", re.I),
    "the engagement folder": re.compile("convey" + "anc", re.I),
    "the private phone-chat adapter": re.compile(r"floor[- ](inbox|chat)\b", re.I),
    "a home path": re.compile(r"/home/[a-z]"),
    "a tailnet address": re.compile(r"\b100\.(6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3}\b"),
    "a LAN address": re.compile(r"\b192\.168\.\d{1,3}\.\d{1,3}\b"),
    "a private package id": re.compile(r"\bpk\.az" + r"an\."),
}

# A device serial is never written here, even in halves: any 11-character token is
# hashed and compared with the serial's digest.
TOKEN_RX = re.compile(r"\b[A-Z0-9]{11}\b")
DIGESTS = {"a device serial": "0d9c5406cea6c61f0161a50620d076e54847b890a37a0b117c6575f1609a2e66"}


def shipped_files():
    for p in sorted(APP.rglob("*")):
        if not p.is_file() or p.resolve() == SELF:
            continue
        if SKIP_DIRS.intersection(p.relative_to(APP).parts) or p.suffix in SKIP_SUFFIX:
            continue
        yield p


class PrivateStringsTests(unittest.TestCase):
    def test_no_private_string_in_the_shipped_tree(self):
        hits = []
        for p in shipped_files():
            try:
                text = p.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            rel = p.relative_to(APP)
            for n, line in enumerate(text.splitlines(), 1):
                for what, rx in PATTERNS.items():
                    # Vendored icons keep their upstream authors' export paths.
                    # Every other pattern still applies under vendor/.
                    if what == "a home path" and "vendor" in rel.parts and rel.parts[:3] == ("src", "metabot_app", "vendor"):
                        continue
                    if rx.search(line):
                        hits.append(f"{rel}:{n}: {what}")
                for tok in TOKEN_RX.findall(line):
                    d = hashlib.sha256(tok.encode()).hexdigest()
                    for what, want in DIGESTS.items():
                        if d == want:
                            hits.append(f"{rel}:{n}: {what}")
        self.assertEqual(hits, [], "\n" + "\n".join(hits[:80]))


if __name__ == "__main__":
    unittest.main()
