#!/usr/bin/env bash
set -euo pipefail
LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"
PLISTS=(
    "com.aiintegraterz.cold-email.slack-agent"
    "com.aiintegraterz.cold-email.reply-loop"
)
for label in "${PLISTS[@]}"; do
    dst="$LAUNCH_AGENTS_DIR/${label}.plist"
    if [[ -f "$dst" ]]; then
        launchctl unload "$dst" 2>/dev/null || true
        rm -f "$dst"
        echo "  • removed $label"
    fi
done
echo "Done. Daemons no longer auto-start."
