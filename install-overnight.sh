#!/bin/bash
# Idempotent install of the 7AM overnight-digest LaunchAgent.
#   ./install-overnight.sh          load/reload the agent
#   ./install-overnight.sh uninstall  remove it
set -euo pipefail

LABEL="com.fnp.overnight-digest"
HERE="$(cd "$(dirname "$0")" && pwd)"
SRC="$HERE/$LABEL.plist"
DST="$HOME/Library/LaunchAgents/$LABEL.plist"

if [[ "${1:-}" == "uninstall" ]]; then
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    rm -f "$DST"
    echo "uninstalled $LABEL"
    exit 0
fi

mkdir -p "$HOME/Library/LaunchAgents" "$HERE/overnight"
cp "$SRC" "$DST"

# Reload cleanly (bootout is a no-op if not loaded).
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$DST"
echo "loaded $LABEL — runs daily at 07:00"
echo "run now:  launchctl start $LABEL"
echo "logs:     $HERE/overnight/overnight.log"
