#!/bin/sh
# Install the demo plugins, save the watchdog loop, add and approve the workflows.
# usage: tools/demo/setup.sh            (run it in the folder the runs should work in,
#        or set DEMO_FOLDER)
# Then: metabot serve   (or Start serve in the app) so the loop fires on its own.
set -eu
. "$(dirname "$0")/env.sh"

echo "== plugins from $DEMO_PLUGINS"
"$METABOT" plugin marketplace add "$DEMO_MARKET" "$DEMO_PLUGINS"
for p in watchdog monitor local-harness browser; do
  "$METABOT" plugin install "$p@$DEMO_MARKET" --yes
done
"$METABOT" plugin enable watchdog --allow-bridges
"$METABOT" plugin enable monitor --add-servers
"$METABOT" plugin enable local-harness --allow-bridges
"$METABOT" plugin enable browser

echo "== the watchdog loop (every 5 minutes once serve runs)"
"$METABOT" routine save watchdog --cron '*/5 * * * *' -e watchdog --cwd "$DEMO_FOLDER" watch

echo "== workflows"
MON=$(monitor_connection)
[ -n "$MON" ] || MON=monitor-monitor
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
for w in inception tiers; do
  python3 -I "$DEMO_DIR/render.py" "$DEMO_DIR/workflows/$w.json" "$DEMO_FOLDER" "$MON" > "$TMP/$w.json"
  "$METABOT" workflow add "$TMP/$w.json"
done

cat <<TXT

done. An approval is your word, given at a terminal (or the app's Approve workflow desk), never a script's:
  metabot workflow approve inception
  metabot workflow approve tiers
Then:
  tools/demo/loop.sh once            fire the watchdog now and read its receipt
  metabot serve                      let the loop fire every 5 minutes (or Start serve in the app)
  tools/demo/run.sh medium "task"    a Cursor run at a tier (low|medium|high|xhigh)
  metabot workflow run inception     watchdog, then Cursor shows a status card on the monitor
  metabot workflow run tiers         the same question at three Cursor tiers, one chain of receipts
  plugins/browser/bin/browser-connect --origin https://example.com   add the browser (an npm download)
TXT
