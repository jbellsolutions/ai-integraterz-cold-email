#!/usr/bin/env bash
# One-screen health check.
set -euo pipefail
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "=== Daemon supervision (launchd) ==="
for label in com.aiintegraterz.cold-email.slack-agent com.aiintegraterz.cold-email.reply-loop; do
    line=$(launchctl list | grep "$label" || true)
    if [[ -z "$line" ]]; then
        echo "  ✗ $label  not loaded"
    else
        pid=$(echo "$line" | awk '{print $1}')
        last_exit=$(echo "$line" | awk '{print $2}')
        if [[ "$pid" == "-" ]]; then
            echo "  ⚠ $label  not running (last exit code: $last_exit)"
        else
            echo "  ✓ $label  running, PID $pid (last exit: $last_exit)"
        fi
    fi
done

echo ""
echo "=== Recent log: slack agent (last 12 lines) ==="
tail -12 "$REPO_DIR/logs/slack_agent.launchd.log" 2>/dev/null || echo "  (no log yet)"

echo ""
echo "=== Recent log: reply loop (last 8 lines) ==="
tail -8 "$REPO_DIR/logs/reply_loop.launchd.log" 2>/dev/null || echo "  (no log yet)"
