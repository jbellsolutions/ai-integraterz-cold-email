#!/usr/bin/env bash
# Wrapper: source .env into the environment, then exec the supplied command.
# Used by launchd plists so the daemons get ANTHROPIC_API_KEY / SLACK_BOT_TOKEN
# / etc. without the secrets having to live in the .plist (which would be
# read-world-readable in ~/Library/LaunchAgents).
#
# Usage: run-with-env.sh <python> -u -m <module> [args...]

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

if [[ -f "$REPO_DIR/.env" ]]; then
    set -a
    # shellcheck disable=SC1091
    source "$REPO_DIR/.env"
    set +a
fi

# launchd captures stdout/stderr; just exec the supplied command.
exec "$@"
