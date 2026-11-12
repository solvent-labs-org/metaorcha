#!/usr/bin/env bash
# E1: assemble the on-device payload tree the APK ships.
#   lib/arm64-v8a/    libpython3.14.so + libssl/libcrypto/libsqlite3_python.so from the
#                     locked CPython tarball, and the vendored launcher under its three
#                     names (it dispatches on basename; payload/launcher/main.c)
#   assets/py/stdlib.zip   the Android stdlib incl. lib-dynload, trimmed (spike-proven set)
#   assets/py/wheels/      the locked pure wheels + our three wheels built from source
#                          (core + sdk from $MB_CORE_ROOT, read-only; the app from this tree)
#   assets/py/site/        yaml from the locked sdist, and the labelled pure-Ed25519
#                          module (android/payload/crypto-pure-ed25519/) with its NOTICE
#   assets/py/payload.list the manifest: <path> <sha256> <bytes> for every payload file,
#                          paths relative to assets/py; the first-run unpacker verifies
#                          every file against it (PayloadAssets.java)
# All third-party inputs come from fetch-payload.sh (cache, lock-verified). Output is a
# fixed literal scratch path, removed and rebuilt whole. Nothing is written elsewhere.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ANDROID="$(cd "$HERE/.." && pwd)"
APP="$(cd "$ANDROID/.." && pwd)"
: "${MB_CORE_ROOT:=$HOME/orcha-040}"
CACHE="${MB_PAYLOAD_CACHE:-${MB_CACHE:-/var/tmp/metabot-app/cache}/android-payload}"
LOCK="${MB_WHEEL_LOCK:-$APP/packaging/android-wheels.lock}"
UV="${UV:-$(command -v uv || echo "$HOME/.local/bin/uv")}"
OUT=/var/tmp/metabot-app/payload-stage

if [ ! -d "$MB_CORE_ROOT/apps/metabot" ] || [ ! -d "$MB_CORE_ROOT/sdk" ]; then
  echo "assemble-payload: MB_CORE_ROOT ($MB_CORE_ROOT) has no apps/metabot + sdk;" >&2
  echo "point it at a read-only core checkout (0.4.0-frozen)" >&2
  exit 1
fi

"$HERE/fetch-payload.sh"

rm -rf /var/tmp/metabot-app/payload-stage
mkdir -p "$OUT/lib/arm64-v8a" "$OUT/assets/py/wheels" "$OUT/assets/py/site"
STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT

# --- CPython runtime: four shared libraries and a trimmed stdlib zip -------------
CPY=$(awk '$1=="cpython"{print $2}' "$LOCK")
tar -xzf "$CACHE/$CPY" -C "$STAGE"
for so in libpython3.14.so libssl_python.so libcrypto_python.so libsqlite3_python.so; do
  cp "$STAGE/prefix/lib/$so" "$OUT/lib/arm64-v8a/$so"
done
mkdir -p "$STAGE/zip/lib"
rsync -a --exclude '__pycache__' --exclude 'test' --exclude 'idlelib' --exclude 'tkinter' \
  --exclude 'turtledemo' --exclude 'turtle.py' --exclude 'ensurepip' --exclude 'lib2to3' \
  --exclude 'pydoc_data' --exclude 'lib-dynload/_test*' --exclude 'lib-dynload/xx*' \
  --exclude 'lib-dynload/_xx*' --exclude 'config-3.14-*' \
  "$STAGE/prefix/lib/python3.14/" "$STAGE/zip/lib/python3.14/"
(cd "$STAGE/zip" && zip -q -r -X "$OUT/assets/py/stdlib.zip" lib)

# --- the launcher under its three names ------------------------------------------
for name in libmetabot_py.so libmetabot_cli.so liborcha_cli.so; do
  cp "$ANDROID/payload/launcher/libmetabot_py.so" "$OUT/lib/arm64-v8a/$name"
done

# --- wheels: the locked pure ones, then our three built from source ---------------
while read -r kind file _rest; do
  case "$kind" in ''|\#*) continue ;; esac
  if [ "$kind" = "wheel" ]; then
    cp "$CACHE/$file" "$OUT/assets/py/wheels/$file"
  fi
done < "$LOCK"

build_wheel() {  # project-dir — one wheel into assets/py/wheels
  # Offline by default (the maintainer machine has the backend cached); CI sets
  # MB_UV_OFFLINE=0 so uv may fetch the build backend on a cold cache.
  local d offline
  d=$(mktemp -d)
  offline="--offline"
  if [ "${MB_UV_OFFLINE:-1}" = "0" ]; then offline=""; fi
  (cd "$1" && "$UV" build --wheel $offline --out-dir "$d" >/dev/null)
  cp "$d"/*.whl "$OUT/assets/py/wheels/"
  rm -rf "$d"
}
build_wheel "$MB_CORE_ROOT/apps/metabot"
build_wheel "$MB_CORE_ROOT/sdk"
build_wheel "$APP"

# --- loose pure packages -----------------------------------------------------------
SD=$(awk '$1=="sdist"{print $2}' "$LOCK")
ver="${SD#pyyaml-}"; ver="${ver%.tar.gz}"
tar -xzf "$CACHE/$SD" -C "$OUT/assets/py/site" --strip-components=2 "pyyaml-$ver/lib/yaml"
rsync -a --exclude '__pycache__' --exclude '*.pyc' \
  "$ANDROID/payload/crypto-pure-ed25519/cryptography/" "$OUT/assets/py/site/cryptography/"
cp "$ANDROID/payload/NOTICE-crypto-pure-ed25519.txt" "$OUT/assets/py/site/"

# --- the manifest -------------------------------------------------------------------
(cd "$OUT/assets/py" && find . -type f ! -name payload.list -printf '%P\n' | LC_ALL=C sort \
  | while read -r f; do
      printf '%s %s %s\n' "$f" "$(sha256sum "$f" | cut -d' ' -f1)" "$(wc -c < "$f" | tr -d ' ')"
    done > payload.list)

echo "payload at $OUT"
du -sh "$OUT/lib" "$OUT/assets/py"
printf 'manifest entries: %s\n' "$(wc -l < "$OUT/assets/py/payload.list")"
