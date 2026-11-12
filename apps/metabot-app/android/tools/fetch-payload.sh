#!/usr/bin/env bash
# E1: every artifact the Android payload ships, in the cache and matching the lock
# (packaging/android-wheels.lock). Cache-first: a cached file is accepted only when
# its size and SHA-256 match the lock; anything fetched is verified the same way
# before it moves into the cache. The launcher is vendored in the tree
# (android/payload/launcher/) and verified, never fetched. `local` entries are our
# own wheels, built by assemble-payload.sh — skipped here.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ANDROID="$(cd "$HERE/.." && pwd)"
LOCK="${MB_WHEEL_LOCK:-$ANDROID/../packaging/android-wheels.lock}"
CACHE="${MB_PAYLOAD_CACHE:-${MB_CACHE:-/var/tmp/metabot-app/cache}/android-payload}"

fail() { echo "fetch-payload: $*" >&2; exit 1; }
[ -f "$LOCK" ] || fail "lock not found: $LOCK"
mkdir -p "$CACHE"

verify() {  # path sha256 bytes
  [ -f "$1" ] || return 1
  [ "$(wc -c < "$1" | tr -d ' ')" = "$3" ] || return 1
  [ "$(sha256sum "$1" | cut -d' ' -f1)" = "$2" ] || return 1
}

pypi_fetch() {  # name file — resolve the exact pinned file on the simple index
  local name file url
  name="$(printf '%s' "$1" | tr 'A-Z' 'a-z' | tr '_' '-')"
  file="$2"
  url=$(curl -fsSL --max-time 30 "https://pypi.org/simple/$name/" \
    | grep -o "href=\"[^\"]*$file\(#[^\"]*\)\?\"" | head -1 \
    | sed 's/^href="//; s/"$//') || true
  [ -n "${url:-}" ] || fail "pypi: $file not found on the $name index"
  curl -fsSL --max-time 300 "$url" -o "$CACHE/$file.part"
}

while read -r kind file sha bytes source; do
  case "$kind" in ''|\#*) continue ;; esac
  case "$kind" in
    local)
      continue ;;
    launcher)
      verify "$ANDROID/payload/launcher/$file" "$sha" "$bytes" \
        || fail "vendored launcher $file drifted from the lock"
      echo "ok $file (vendored)"
      continue ;;
    cpython|wheel|sdist) : ;;
    *) fail "unknown kind $kind" ;;
  esac
  if verify "$CACHE/$file" "$sha" "$bytes"; then
    echo "ok $file (cache)"
    continue
  fi
  rm -f "$CACHE/$file" "$CACHE/$file.part"
  if [ "$kind" = "cpython" ]; then
    curl -fsSL --max-time 600 "$source" -o "$CACHE/$file.part"
  else
    pypi_fetch "${file%%-*}" "$file"
  fi
  if verify "$CACHE/$file.part" "$sha" "$bytes"; then
    mv "$CACHE/$file.part" "$CACHE/$file"
    echo "fetched $file"
  else
    rm -f "$CACHE/$file.part"
    fail "$file does not match the lock after fetch"
  fi
done < "$LOCK"
