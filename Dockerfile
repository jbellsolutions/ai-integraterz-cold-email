# Cold Email 2.0 — minimal container for the Slack agent + reply daemon.
# Built for environments where running the watchdog on a Mac isn't viable.
# This is a SIMPLE deploy target; the daemon still polls (no webhook).
#
# Build:  docker build -t cold-email-2 .
# Run:    docker run --env-file .env cold-email-2
#
# Note: the npm Smartlead CLI must be available at /usr/local/bin/smartlead
# (this image installs it).

FROM python:3.13-slim

# System deps + Node for the Smartlead CLI
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates gnupg \
 && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
 && apt-get install -y nodejs \
 && rm -rf /var/lib/apt/lists/*

# Smartlead CLI
RUN npm install -g @smartlead/cli

WORKDIR /app

# Python deps (cached as a layer)
COPY pyproject.toml ./
RUN pip install --no-cache-dir -e . && \
    pip install --no-cache-dir git+https://github.com/jbellsolutions/forge

# App code
COPY orchestrator orchestrator
COPY squads squads
COPY tools tools
COPY config config
COPY campaigns campaigns
COPY ops ops
COPY tests tests
COPY *.md *.toml *.txt *.json VERSION ./

# Run the watchdog as PID 1; it spawns slack_agent + reply_loop and supervises both
RUN chmod +x ops/*.sh

# Heartbeat is the liveness signal; restart on exit
HEALTHCHECK --interval=60s --timeout=10s --start-period=20s --retries=3 \
    CMD ps aux | grep -E "slack_agent|reply_loop" | grep -v grep > /dev/null || exit 1

CMD ["./ops/watchdog.sh"]
