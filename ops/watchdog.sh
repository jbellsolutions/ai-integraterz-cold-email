#!/usr/bin/env bash
# Foreground watchdog: keeps both daemons alive without launchd.
#
# Why this exists: launchd-supervised daemons fail under macOS TCC (Full Disk
# Access) when the repo lives in ~/Desktop. This watchdog runs from a normal
# Terminal session (which has user-level Desktop access by default) and
# restarts each daemon if it dies. Crash-loop throttled at 30s.
#
# Compatible with macOS bash 3.2 (no associative arrays).
#
# Usage:
#   ./ops/watchdog.sh                                  — foreground (CTRL-C to stop)
#   nohup ./ops/watchdog.sh > logs/watchdog.log 2>&1 & — background, survives shell close
#   caffeinate -i ./ops/watchdog.sh                    — prevent sleep + run
#
# True always-on across reboots — three options:
#   1. Add this to login items (System Settings → General → Login Items)
#      and wrap with `caffeinate` so the Mac doesn't sleep.
#   2. Move the repo to ~/cold-email-2.0 and use launchd (no TCC issues).
#   3. Run inside `tmux new-session -d -s cold-email './ops/watchdog.sh'`
#      so it survives terminal close and you can re-attach to inspect.

set -uo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

if [[ -f "$REPO_DIR/.env" ]]; then
    set -a
    # shellcheck disable=SC1091
    source "$REPO_DIR/.env"
    set +a
fi

mkdir -p "$REPO_DIR/logs"

PYTHON="$REPO_DIR/.venv/bin/python"

# Two daemons. Per-daemon state stored in parallel arrays (bash 3.2 friendly).
NAMES=(slack_agent reply_loop)
ARGS_slack_agent="-u -m orchestrator.slack_agent --no-announce"
ARGS_reply_loop="-u -m orchestrator.reply_loop --interval=60 --angle=power-partner"

PID_slack_agent=0
PID_reply_loop=0
LAST_RESTART_slack_agent=0
LAST_RESTART_reply_loop=0

start_daemon() {
    local name="$1"
    local args_var="ARGS_$name"
    local args="${!args_var}"
    local logfile="$REPO_DIR/logs/${name}.watchdog.log"
    # The nohup utility can't handle spaces in the redirect target's parent
    # path on older macOS bash — use exec-redirect via subshell instead.
    # shellcheck disable=SC2086
    ( nohup "$PYTHON" $args >> "$logfile" 2>&1 ) &
    local pid=$!
    eval "PID_${name}=$pid"
    echo "$(date '+%H:%M:%S')  ✓ started $name (PID $pid) → logs/${name}.watchdog.log"
}

cleanup() {
    echo ""
    echo "$(date '+%H:%M:%S')  watchdog stopping; killing children…"
    for n in "${NAMES[@]}"; do
        local pid_var="PID_$n"
        kill "${!pid_var}" 2>/dev/null || true
    done
    exit 0
}
trap cleanup INT TERM

echo "$(date '+%H:%M:%S')  Cold Email 2.0 watchdog starting (PID $$)"
for n in "${NAMES[@]}"; do
    start_daemon "$n"
done

# Supervision loop — every 15s, check each PID; restart if dead.
while true; do
    sleep 15
    for n in "${NAMES[@]}"; do
        pid_var="PID_$n"
        last_var="LAST_RESTART_$n"
        pid="${!pid_var}"
        if ! kill -0 "$pid" 2>/dev/null; then
            now=$(date +%s)
            last="${!last_var}"
            if (( now - last < 30 )); then
                echo "$(date '+%H:%M:%S')  ⚠ $n died but throttled (last restart ${last}, now ${now})"
                continue
            fi
            echo "$(date '+%H:%M:%S')  ⚠ $n (PID $pid) died — restarting"
            start_daemon "$n"
            eval "LAST_RESTART_${n}=$now"
        fi
    done
done
