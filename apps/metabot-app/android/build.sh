#!/usr/bin/env bash
# Generic MetaBot APK. No Gradle. classes.dex is stored before zipalign.
# The E1 on-device payload (CPython, stdlib.zip, locked wheels, our three wheels) is
# assembled under packaging/android-wheels.lock by tools/assemble-payload.sh and ships
# in lib/arm64-v8a + assets/py; native libraries are stored and page-aligned, the
# strings check covers the whole APK, and the build fails above the 60 MB cap.
# Passwords come from the environment and are never printed.
# MB_DEBUGGABLE=1 builds a debuggable APK (the pairing screen then honours the scripted-walk
# intent extras, tools/tablet-e2e.sh); MB_VERSION_CODE / MB_VERSION_NAME set the version.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
SDK="${ANDROID_SDK_ROOT:-$HOME/Android/Sdk}"
BT="$SDK/build-tools/34.0.0"
PLATFORM="$SDK/platforms/android-34/android.jar"
# ZXing core is fetched once from Maven Central and pinned by SHA-256; no jar lives in git.
ZX_URL="https://repo1.maven.org/maven2/com/google/zxing/core/3.5.3/core-3.5.3.jar"
ZX_SHA256="8d8064c1636fdaef7189dd9055c7d59950a8940a12f2293956446ec3c109fd82"
ZX="${MB_CACHE:-${XDG_CACHE_HOME:-$HOME/.cache}/metabot-app}/zxing-core-3.5.3.jar"
export PATH="${JAVA_HOME:-$HOME/.local/lib/jdk}/bin:$PATH"
OUT="${MB_OUT:-/var/tmp/metabot-app/apk}"

avail_kb=$(df -Pk / | awk 'NR==2 {print $4}')
if [ "$avail_kb" -lt 4194304 ]; then
  echo "under 4 GB free" >&2
  exit 1
fi

fetch_zxing() {
  if [ -f "$ZX" ] && echo "$ZX_SHA256  $ZX" | sha256sum -c --status; then return 0; fi
  mkdir -p "$(dirname "$ZX")"
  curl -fsSL "$ZX_URL" -o "$ZX.part"
  if ! echo "$ZX_SHA256  $ZX.part" | sha256sum -c --status; then
    rm -f "$ZX.part"
    echo "zxing jar checksum mismatch" >&2
    exit 1
  fi
  mv "$ZX.part" "$ZX"
}

if [ "${1:-}" = "test" ]; then
  shift
  W=$(mktemp -d)
  trap 'rm -rf "$W"' EXIT
  javac --release 17 -d "$W" \
    "$ROOT/src/ai/metaorcha/metabot/Fingerprint.java" \
    "$ROOT/src/ai/metaorcha/metabot/Words.java" \
    "$ROOT/src/ai/metaorcha/metabot/Failover.java" \
    "$ROOT/src/ai/metaorcha/metabot/Pairing.java" \
    "$ROOT/src/ai/metaorcha/metabot/WheelLock.java" \
    "$ROOT/src/ai/metaorcha/metabot/PayloadAssets.java" \
    "$ROOT/test/RunTests.java"
  java -cp "$W" ai.metaorcha.metabot.RunTests "$@"
  exit 0
fi

if [ -z "${MB_KEYSTORE:-}" ] || [ -z "${MB_KS_PASS:-}" ] || [ -z "${MB_KEY_PASS:-}" ]; then
  echo "MB_KEYSTORE, MB_KS_PASS and MB_KEY_PASS must be set" >&2
  exit 1
fi
if [ ! -f "$MB_KEYSTORE" ]; then
  echo "keystore file is missing" >&2
  exit 1
fi
fetch_zxing
if [ ! -f "$PLATFORM" ] || [ ! -x "$BT/aapt2" ]; then
  echo "Android SDK platform 34 or build-tools 34.0.0 is missing" >&2
  exit 1
fi

# The on-device Python payload (E1): locked artifacts fetched to the cache, our wheels
# built, the tree staged whole at /var/tmp/metabot-app/payload-stage with its manifest.
"$ROOT/tools/assemble-payload.sh"
PAYLOAD=/var/tmp/metabot-app/payload-stage

W=$(mktemp -d)
trap 'rm -rf "$W"' EXIT
mkdir -p "$W/res" "$W/gen" "$W/classes" "$W/assets"
cp -a "$ROOT/res/." "$W/res/"
cp "$ROOT/assets/pair.html" "$W/assets/pair.html"

"$BT/aapt2" compile --dir "$W/res" -o "$W/res.zip"
"$BT/aapt2" link -o "$W/base.apk" --manifest "$ROOT/AndroidManifest.xml" -I "$PLATFORM" \
  --java "$W/gen" \
  --min-sdk-version 24 --target-sdk-version 35 \
  --version-code "${MB_VERSION_CODE:-1}" --version-name "${MB_VERSION_NAME:-1.0.0}" \
  ${MB_DEBUGGABLE:+--debug-mode} \
  "$W/res.zip"

javac --release 17 -cp "$PLATFORM:$ZX" -d "$W/classes" \
  $(find "$ROOT/src" "$W/gen" -name '*.java')
# d8 8.2.2 NPEs on MethodParameters entries whose name index is 0
# (synthetic constructors of inner and anonymous classes from this javac).
python3 - "$W/classes" << 'PY'
import struct, sys
from pathlib import Path
def u2(b, i):
    return struct.unpack_from(">H", b, i)[0], i + 2
def u4(b, i):
    return struct.unpack_from(">I", b, i)[0], i + 4
def parse_cp(b, i):
    count, i = u2(b, i)
    cp = [None]
    n = 1
    while n < count:
        tag = b[i]; i += 1
        if tag == 1:
            ln, i = u2(b, i)
            cp.append(b[i:i+ln]); i += ln
        elif tag in (7, 8, 16, 19, 20):
            i += 2; cp.append(None)
        elif tag in (3, 4, 9, 10, 11, 12, 17, 18):
            i += 4; cp.append(None)
        elif tag in (5, 6):
            i += 8; cp.append(None); cp.append(None); n += 1
        elif tag == 15:
            i += 3; cp.append(None)
        else:
            raise SystemExit("bad class tag %s" % tag)
        n += 1
    return cp, i
def skip_attrs(b, i, drop):
    ac, i = u2(b, i)
    kept = []
    for _ in range(ac):
        name, i = u2(b, i)
        ln, i = u4(b, i)
        body, i = b[i:i+ln], i + ln
        if name != drop:
            kept.append((name, body))
    return kept, i
def strip(path):
    b = path.read_bytes()
    if b[:4] != b"\xca\xfe\xba\xbe":
        return
    i = 8
    cp, i = parse_cp(b, i)
    drop = next((idx for idx, item in enumerate(cp) if item == b"MethodParameters"), None)
    if drop is None:
        return
    head = b[:i]
    i += 6
    ic, i = u2(b, i)
    i += 2 * ic
    out = bytearray(b[len(head):i])
    def members(i, out):
        mc, i = u2(b, i)
        out += struct.pack(">H", mc)
        for _ in range(mc):
            out += b[i:i+6]; i += 6
            kept, i = skip_attrs(b, i, drop)
            out += struct.pack(">H", len(kept))
            for name, body in kept:
                out += struct.pack(">H", name) + struct.pack(">I", len(body)) + body
        return i
    i = members(i, out)
    i = members(i, out)
    kept, i = skip_attrs(b, i, drop)
    out += struct.pack(">H", len(kept))
    for name, body in kept:
        out += struct.pack(">H", name) + struct.pack(">I", len(body)) + body
    if i != len(b):
        raise SystemExit("class rewrite stopped early: %s" % path)
    path.write_bytes(head + out)
root = Path(sys.argv[1])
for p in root.rglob("*.class"):
    strip(p)
PY
"$BT/d8" --lib "$PLATFORM" --min-api 24 --output "$W" $(find "$W/classes" -name '*.class') "$ZX"

# Dex goes in before zipalign. Signing comes after alignment. The payload's native
# libraries are stored (never compressed) so zipalign can page-align them; the zips
# (stdlib, wheels) gain nothing from a second deflate, so they are stored too.
(cd "$W" && zip -q -0 base.apk classes.dex && zip -q base.apk assets/pair.html)
(cd "$PAYLOAD" && zip -q -0 -r "$W/base.apk" lib/arm64-v8a assets/py/stdlib.zip assets/py/wheels \
  && zip -q -r "$W/base.apk" assets/py/site assets/py/payload.list)
"$BT/zipalign" -f -p 4 "$W/base.apk" "$W/aligned.apk"
"$BT/zipalign" -c -p 4 "$W/aligned.apk"
"$BT/apksigner" sign --ks "$MB_KEYSTORE" \
  --ks-pass env:MB_KS_PASS --key-pass env:MB_KEY_PASS \
  --out "$W/final.apk" "$W/aligned.apk"
"$BT/apksigner" verify "$W/final.apk"

python3 - "$W/final.apk" "$ROOT/../packaging/android-wheels.lock" << 'PY'
import io, re, sys, zipfile
apk = zipfile.ZipFile(sys.argv[1])
# The payload must be whole: the four runtime libraries, the launcher under its three
# names, stdlib.zip, the manifest, and exactly the locked wheels plus our three.
names = set(apk.namelist())
need = ["assets/py/stdlib.zip", "assets/py/payload.list",
        "lib/arm64-v8a/libpython3.14.so", "lib/arm64-v8a/libssl_python.so",
        "lib/arm64-v8a/libcrypto_python.so", "lib/arm64-v8a/libsqlite3_python.so",
        "lib/arm64-v8a/libmetabot_py.so", "lib/arm64-v8a/libmetabot_cli.so",
        "lib/arm64-v8a/liborcha_cli.so"]
missing = [n for n in need if n not in names]
locked = sum(1 for line in open(sys.argv[2]) if line.startswith("wheel "))
wheels = [n for n in names if n.startswith("assets/py/wheels/") and n.endswith(".whl")]
if len(wheels) != locked + 3:
    missing.append("assets/py/wheels: %d wheels, want %d locked + 3 ours" % (len(wheels), locked))
if missing:
    sys.stderr.write("APK payload incomplete: %s\n" % ", ".join(missing))
    sys.exit(1)
TOKEN = re.compile(r"\?t=[A-Za-z0-9_-]{8,}")
IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
# Every entry is scanned, unzipping nested zips (stdlib.zip, the wheels) so a baked
# token in payload Python source is seen. A token would read "?t=" followed by token
# characters; the bare query name the code appends at runtime is fine. The IPv4 rule
# stays dex-only: the stdlib and Python sources hold example addresses legitimately.
def strings(data):
    return b"\n".join(re.findall(rb"[\x20-\x7e]{4,}", data)).decode("ascii", "replace")
def blobs(name, data, out):
    if data[:4] == b"PK\x03\x04":
        try:
            z = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile:
            out.append((name, data))
            return
        for n in z.namelist():
            if not n.endswith("/"):
                blobs(name + "!" + n, z.read(n), out)
    else:
        out.append((name, data))
bad = []
for info in apk.infolist():
    if info.is_dir():
        continue
    entries = []
    blobs(info.filename, apk.read(info.filename), entries)
    for name, data in entries:
        text = strings(data)
        for m in TOKEN.findall(text):
            bad.append("%s: %s" % (name, m))
        if info.filename == "classes.dex":
            for m in IPV4.findall(text):
                bad.append("%s: %s" % (name, m))
if bad:
    sys.stderr.write("forbidden strings in the APK:\n" + "\n".join(bad) + "\n")
    sys.exit(1)
print("strings check ok (whole APK)")
PY

# The 60 MB cap is a hard fail: above it the build does not ship.
apk_bytes=$(wc -c < "$W/final.apk" | tr -d ' ')
if [ "$apk_bytes" -gt 62914560 ]; then
  echo "APK over the 60 MB cap: $apk_bytes bytes" >&2
  exit 1
fi

mkdir -p "$OUT"
cp "$W/final.apk" "$OUT/ai.metaorcha.metabot.apk"
"$BT/aapt" dump badging "$OUT/ai.metaorcha.metabot.apk" | head -5
echo "APK_BYTES $(wc -c < "$OUT/ai.metaorcha.metabot.apk")"
echo "BUILT $OUT/ai.metaorcha.metabot.apk"
