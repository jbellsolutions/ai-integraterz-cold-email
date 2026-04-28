#!/usr/bin/env bash
# Install the launchd supervisors for the slack agent + reply daemon.
#
# After running this, the daemons will:
#   • start automatically when you log in
#   • restart automatically if they crash
#   • survive Mac restarts (start at next login)
#   • write logs to logs/*.launchd.log under the repo
#
# Usage:
#   ./ops/install.sh        — install + load (starts both)
#   ./ops/uninstall.sh      — unload + remove
#   ./ops/status.sh         — show running state + recent log
#
# Install is idempotent — running it again replaces the plists with current
# repo paths and reloads.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"
mkdir -p "$LAUNCH_AGENTS_DIR" "$REPO_DIR/logs"

# Make wrapper executable
chmod +x "$REPO_DIR/ops/run-with-env.sh"

PLISTS=(
    "com.aiintegraterz.cold-email.slack-agent"
    "com.aiintegraterz.cold-email.reply-loop"
)

for label in "${PLISTS[@]}"; do
    src="$REPO_DIR/ops/${label}.plist"
    dst="$LAUNCH_AGENTS_DIR/${label}.plist"

    # Stop + remove existing if present (so reinstall is clean)
    if launchctl list | grep -q "$label"; then
        echo "  • stopping existing $label"
        launchctl unload "$dst" 2>/dev/null || true
    fi

    # Substitute repo path placeholder + write
    sed "s|__REPO__|$REPO_DIR|g" "$src" > "$dst"
    chmod 644 "$dst"
    echo "  • installed → $dst"

    # Load + start
    launchctl load -w "$dst"
    sleep 2
    if launchctl list | grep -q "$label"; then
        pid=$(launchctl list | grep "$label" | awk '{print $1}')
        echo "  • $label running (PID $pid) ✓"
    else
        echo "  ⚠ $label did not start; check logs/${label#com.aiintegraterz.cold-email.}.launchd.log"
    fi
done

echo ""
echo "Daemons supervised. They'll auto-restart on crash and start on login."
echo "Logs:  tail -f $REPO_DIR/logs/*.launchd.log"
echo "Status: $REPO_DIR/ops/status.sh"
echo "Stop:   $REPO_DIR/ops/uninstall.sh"
