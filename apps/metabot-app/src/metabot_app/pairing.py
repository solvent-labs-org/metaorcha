"""C4: pairing a phone.

The desktop starts a code (``POST /api/pair/start``, loopback only): 8 Crockford
base32 characters, good for ``TTL`` seconds, one live code at a time. The QR (or
the typed address + code) carries ``metabot-pair:1?a=<addresses>&p=<port>&c=<code>
&f=<fingerprint>``; the phone pins the fingerprint, then claims the code over the
TLS listener (``POST /api/pair``, unauthenticated) and gets a device token of its
own (C3). A code dies after ``TRIES`` wrong claims or one right one. The four words
shown on both screens are the APK's: the first four fingerprint bytes indexed into
``WORDS``.
"""
from __future__ import annotations

import secrets
import threading
import time
from urllib.parse import quote

from . import devices as devices_mod

ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"   # Crockford base32: no I, L, O, U
CODE_LEN = 8
TTL = 600
TRIES = 5

# Kept identical to the APK's Words.java (256 entries): a change here is a protocol change.
WORDS = (
    "apple river stone cloud maple cedar birch pine oak elm ash willow reed moss fern ivy "
    "vine grass leaf bark twig root seed bloom petal fruit berry grain wheat corn rice oat "
    "horse sheep goat cow bird duck goose swan dove hawk owl crane robin sparrow eagle whale "
    "shark trout bass perch frog snake lizard bear wolf fox deer hare rabbit mouse squirrel otter "
    "bread milk honey salt sugar butter cheese egg water fire wind rain snow ice mist fog "
    "sun moon star sky dawn dusk light shade hill lake pond bay sea wave tide sand "
    "path road bridge gate house door room table chair lamp clock book pen paper ink page "
    "happy quiet brave calm kind bold swift clear north south east west spring summer autumn winter "
    "morning night field garden orchard meadow forest mother father sister brother friend child family home music "
    "song dance story poem letter sound color circle square round small large short young early black "
    "white brown yellow orange purple pink grey coffee tea juice soup pizza pasta salad lemon mango "
    "peach plum grape melon olive pepper ginger garlic onion carrot potato tomato noodle roasted baked boiled "
    "fresh crisp sweet sour bitter spicy mild warm cool soft hard rough smooth shiny dull heavy "
    "lightest tiny huge quick slow still bright dim loud softest near far inner outer upper lower "
    "first second third last true false open close start stop left right above below today tomorrow "
    "always never maybe please thanks hello world people place thing point line shape metal wood glass"
).split()
assert len(WORDS) == 256


def normalize(raw) -> str | None:
    """Upper-case, I/L -> 1, O -> 0, separators dropped; None unless 8 Crockford characters."""
    if not isinstance(raw, str):
        return None
    s = raw.strip().upper().replace("-", "").replace(" ", "")
    if len(s) != CODE_LEN:
        return None
    out = []
    for c in s:
        c = {"I": "1", "L": "1", "O": "0"}.get(c, c)
        if c not in ALPHABET:
            return None
        out.append(c)
    return "".join(out)


def payload(addresses, port, code, fingerprint) -> str:
    return (f"metabot-pair:1?a={quote(','.join(addresses), safe='')}&p={int(port)}"
            f"&c={code}&f={fingerprint}")


def words(fingerprint: str) -> list[str]:
    raw = bytes.fromhex(fingerprint)
    return [WORDS[b] for b in raw[:4]]


class Pairing:
    def __init__(self, devices: devices_mod.Devices, ttl: int = TTL):
        self.devices = devices
        self.ttl = ttl
        self._live = None        # {"code", "expires", "tries"}
        self._lock = threading.Lock()

    def _alive(self) -> bool:
        return self._live is not None and time.time() < self._live["expires"]

    def start(self) -> dict:
        code = "".join(secrets.choice(ALPHABET) for _ in range(CODE_LEN))
        with self._lock:
            self._live = {"code": code, "expires": time.time() + self.ttl, "tries": 0}
        return {"code": code, "expires_in": self.ttl}

    def status(self) -> dict:
        with self._lock:
            if not self._alive():
                return {"live": False, "expires_in": 0}
            return {"live": True, "expires_in": int(self._live["expires"] - time.time())}

    def claim(self, code, name) -> tuple[int, dict]:
        """(status, reply). 200 with the device token, else 403 "bad code"."""
        bad = (403, {"ok": False, "error": "bad code"})
        norm = normalize(code)
        if norm is None:
            return bad
        with self._lock:
            if not self._alive():
                self._live = None
                return bad
            if not secrets.compare_digest(norm, self._live["code"]):
                self._live["tries"] += 1
                if self._live["tries"] >= TRIES:
                    self._live = None
                return bad
            self._live = None            # one claim per code
        token, row = self.devices.add(name, "phone")
        return 200, {"ok": True, "token": token, "id": row["id"], "name": row["name"]}
