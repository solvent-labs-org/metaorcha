#!/bin/sh
# Start your own sub-harness: a bridge plugin MetaBot runs as an engine.
# usage: tools/demo/new-harness.sh NAME
set -eu
. "$(dirname "$0")/env.sh"
[ $# -eq 1 ] || die "usage: new-harness.sh NAME"
NAME="$1"
"$METABOT" plugin new "$NAME" --bridge --dir "$DEMO_PLUGINS/$NAME"
cat <<TXT

wrote $DEMO_PLUGINS/$NAME. Put your engine in bin/$NAME (plugins/local-harness/bin/local-harness
is a worked example with a tool loop), list it in plugins/.claude-plugin/marketplace.json, then:
  $METABOT plugin install $NAME@$DEMO_MARKET --yes
  $METABOT plugin enable $NAME --allow-bridges
  $METABOT ask -e $NAME "say hi"
TXT
