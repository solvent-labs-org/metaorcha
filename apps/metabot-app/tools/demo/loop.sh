#!/bin/sh
# The watchdog loop: fire it once, read its firings, or serve so it fires on its own.
# usage: tools/demo/loop.sh once | firings | serve
set -eu
. "$(dirname "$0")/env.sh"
case "${1:-once}" in
  once) exec "$METABOT" routine run watchdog;;
  firings) exec "$METABOT" routine firings watchdog;;
  serve) exec "$METABOT" serve;;
  *) die "usage: loop.sh once | firings | serve";;
esac
