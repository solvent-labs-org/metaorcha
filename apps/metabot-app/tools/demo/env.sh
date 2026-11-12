#!/bin/sh
# Shared by the demo scripts (sourced). Override with the environment:
#   METABOT        the metabot command (default: metabot on PATH)
#   DEMO_FOLDER    the folder the runs work in (default: the current folder)
#   DEMO_CURSOR    the Cursor model family the tiers map to (default: grok-4.7)
#   DEMO_LOCAL_MODEL the local model for the sub-harness (default: qwen2.5:3b)
DEMO_DIR=$(cd "$(dirname "$0")" && pwd)
DEMO_APP=$(cd "$DEMO_DIR/../.." && pwd)
DEMO_PLUGINS="$DEMO_APP/plugins"
DEMO_MARKET=metabot-demos
METABOT="${METABOT:-metabot}"
DEMO_FOLDER="${DEMO_FOLDER:-$PWD}"
DEMO_CURSOR="${DEMO_CURSOR:-grok-4.7}"
DEMO_LOCAL_MODEL="${DEMO_LOCAL_MODEL:-qwen2.5:3b}"

die() { echo "demo: $*" >&2; exit 2; }

# cursor_model TIER -> the model name a tier means, e.g. high -> grok-4.7-high
cursor_model() {
  case "$1" in
    low|medium|high|xhigh) echo "$DEMO_CURSOR-$1";;
    *) die "tier is one of low, medium, high, xhigh (got '$1')";;
  esac
}

# monitor_connection -> the connection the monitor plugin added (empty when none)
monitor_connection() {
  "$METABOT" connect list 2>/dev/null | awk '$1 ~ /^monitor/ {print $1; exit}'
}
