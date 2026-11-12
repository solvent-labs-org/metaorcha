#!/bin/sh
# One run at a Cursor model tier, read-only unless --write.
# usage: tools/demo/run.sh [--engine cursor|local|claude|codex] [--write] TIER TASK...
#   TIER is low, medium, high or xhigh (the model is $DEMO_CURSOR-TIER, grok-4.7 by default);
#   with --engine local the tier is ignored and the local sub-harness runs $DEMO_LOCAL_MODEL.
set -eu
. "$(dirname "$0")/env.sh"
ENGINE=cursor; MODE=--read-only
while [ $# -gt 0 ]; do
  case "$1" in
    --engine) ENGINE="$2"; shift 2;;
    --write) MODE=""; shift;;
    --) shift; break;;
    -*) die "unknown word $1";;
    *) break;;
  esac
done
[ $# -ge 2 ] || die "usage: run.sh [--engine E] [--write] TIER TASK..."
TIER="$1"; shift
case "$ENGINE" in
  cursor) MODEL=$(cursor_model "$TIER"); E=cursor;;
  local) MODEL="$DEMO_LOCAL_MODEL"; E=local-harness;;
  claude|codex) E="$ENGINE"; MODEL="";;
  *) die "engine is cursor, local, claude or codex";;
esac
if [ -n "$MODEL" ]; then
  # shellcheck disable=SC2086
  exec "$METABOT" ask -e "$E" -m "$MODEL" --cwd "$DEMO_FOLDER" $MODE "$@"
fi
# shellcheck disable=SC2086
exec "$METABOT" ask -e "$E" --cwd "$DEMO_FOLDER" $MODE "$@"
