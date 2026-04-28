#!/usr/bin/env bash
set -euo pipefail

# ─────────────────────────────────────────────────────────────────────
# extract-checkpoint.sh — Session checkpoint content extraction
# Called by the Stop hook on every session exit.
# Reads the session transcript, calls Haiku to extract structured
# content, PII-scans the result, and writes the checkpoint.
# Falls back to skeleton-only if API key is missing or call fails.
# ─────────────────────────────────────────────────────────────────────

# Configuration
MODEL="claude-haiku-4-5-20251001"
MAX_TOKENS=500
TIMEOUT=15

# Paths
CHECKPOINT_DIR=".claude/checkpoints"
LATEST="$CHECKPOINT_DIR/latest.json"
STATE_FILE=".agent/state.json"
IDENTITY_FILE=".agent/identity.json"
PII_PATTERNS=".claude/security/pii-patterns.json"

# Create directories
mkdir -p "$CHECKPOINT_DIR" ".agent" ".claude/learning"

# ── Session count (existing behavior) ──────────────────────────────
SESSION_COUNT=0
if [ -f "$STATE_FILE" ]; then
    SESSION_COUNT=$(python3 -c "import json; print(json.load(open('$STATE_FILE')).get('session_count', 0))" 2>/dev/null || echo "0")
fi
SESSION_COUNT=$((SESSION_COUNT + 1))

# Update state.json
python3 -c "
import json, os
state = {}
try: state = json.load(open('$STATE_FILE'))
except: pass
state['session_count'] = $SESSION_COUNT
state['last_session'] = '$(date -u +%Y-%m-%dT%H:%M:%SZ)'
json.dump(state, open('$STATE_FILE', 'w'), indent=2)
" 2>/dev/null || true

# ── Milestone check (existing behavior) ────────────────────────────
if [ $((SESSION_COUNT % 10)) -eq 0 ]; then
    OBS_FILE=".claude/learning/observations.json"
    python3 -c "
import json
obs = []
try: obs = json.load(open('$OBS_FILE'))
except: pass
obs.append({
    'type': 'session_milestone',
    'session_count': $SESSION_COUNT,
    'note': 'Run /agi-learn to process accumulated observations',
    'timestamp': '$(date -u +%Y-%m-%dT%H:%M:%SZ)',
    'source': 'hook-auto'
})
json.dump(obs, open('$OBS_FILE', 'w'), indent=2)
" 2>/dev/null || true
fi

# ── Git state ──────────────────────────────────────────────────────
GIT_BRANCH=$(git branch --show-current 2>/dev/null || echo "unknown")
GIT_COMMIT=$(git rev-parse --short HEAD 2>/dev/null || echo "unknown")
GIT_DIRTY=$(git status --porcelain 2>/dev/null | wc -l | tr -d ' ')
GIT_FILES_CHANGED=$(git diff --name-only HEAD 2>/dev/null | head -20 | python3 -c "
import sys, json
print(json.dumps([l.strip() for l in sys.stdin if l.strip()]))
" 2>/dev/null || echo "[]")

# ── Project name ───────────────────────────────────────────────────
PROJECT_NAME="unknown"
if [ -f "$IDENTITY_FILE" ]; then
    PROJECT_NAME=$(python3 -c "import json; print(json.load(open('$IDENTITY_FILE')).get('project_name', 'unknown'))" 2>/dev/null || echo "unknown")
fi
if [ "$PROJECT_NAME" = "unknown" ]; then
    PROJECT_NAME=$(basename "$(pwd)")
fi

# ── Find session transcript ────────────────────────────────────────
# Claude Code stores transcripts in ~/.claude/projects/ keyed by directory path
TRANSCRIPT=""

# Strategy 1: Find project-specific directory by matching the current path
PROJECT_DIR=""
ENCODED_CWD=$(pwd | sed 's|/|%2F|g')
for dir in ~/.claude/projects/*/; do
    if [ -d "$dir" ]; then
        DIR_BASE=$(basename "$dir")
        # Check if the directory name matches an encoding of our cwd
        if echo "$DIR_BASE" | grep -qi "$(basename "$(pwd)")" 2>/dev/null; then
            PROJECT_DIR="$dir"
            break
        fi
    fi
done

# Strategy 2: If no match by name, try all project dirs for the most recent jsonl
if [ -z "$PROJECT_DIR" ]; then
    LATEST_JSONL=$(find ~/.claude/projects/ -name "*.jsonl" -type f 2>/dev/null | xargs ls -t 2>/dev/null | head -1 || true)
    if [ -n "$LATEST_JSONL" ]; then
        PROJECT_DIR=$(dirname "$LATEST_JSONL")
    fi
fi

# Extract transcript from the most recent jsonl in the project dir
if [ -n "$PROJECT_DIR" ] && [ -d "$PROJECT_DIR" ]; then
    LATEST_JSONL=$(ls -t "$PROJECT_DIR"/*.jsonl 2>/dev/null | head -1 || true)
    if [ -n "$LATEST_JSONL" ] && [ -f "$LATEST_JSONL" ]; then
        # Get last ~50 lines (messages), cap at 30KB to keep API call small
        TRANSCRIPT=$(tail -50 "$LATEST_JSONL" 2>/dev/null | head -c 30000 || true)
    fi
fi

# ── Extract checkpoint content via Haiku ───────────────────────────
INTENT=""
DECISIONS="[]"
FILES_MODIFIED="[]"
NEXT_STEPS="[]"
LESSONS="[]"

if [ -n "$TRANSCRIPT" ] && [ -n "${ANTHROPIC_API_KEY:-}" ]; then
    # Build the extraction prompt
    EXTRACT_PROMPT='Extract a session summary from this Claude Code transcript. Return ONLY valid JSON with these fields:
{
  "intent": "What was the user working on? 1-3 sentences.",
  "decisions": ["Key choice 1", "Key choice 2"],
  "files_modified": ["path/to/file1.py", "path/to/file2.md"],
  "next_steps": ["Specific next action 1", "Specific next action 2"],
  "lessons": ["Timeless insight (only if genuinely new, otherwise empty array)"]
}

Be specific. Name files, functions, and choices. No vague summaries.'

    # Escape transcript for JSON (cap at 25KB for the API payload)
    ESCAPED_TRANSCRIPT=$(echo "$TRANSCRIPT" | python3 -c "
import sys, json
print(json.dumps(sys.stdin.read()[:25000]))
" 2>/dev/null || echo '""')

    # Escape the prompt for JSON
    ESCAPED_PROMPT=$(echo "$EXTRACT_PROMPT" | python3 -c "
import sys, json
print(json.dumps(sys.stdin.read()))
" 2>/dev/null || echo '""')

    # Call Anthropic API with timeout
    RESPONSE=$(curl -s --max-time "$TIMEOUT" \
        -H "x-api-key: $ANTHROPIC_API_KEY" \
        -H "anthropic-version: 2023-06-01" \
        -H "content-type: application/json" \
        -d "{
            \"model\": \"$MODEL\",
            \"max_tokens\": $MAX_TOKENS,
            \"messages\": [
                {\"role\": \"user\", \"content\": $ESCAPED_TRANSCRIPT},
                {\"role\": \"user\", \"content\": $ESCAPED_PROMPT}
            ]
        }" \
        https://api.anthropic.com/v1/messages 2>/dev/null || echo "")

    # Parse response — extract JSON from Haiku's reply
    if [ -n "$RESPONSE" ]; then
        EXTRACTED=$(echo "$RESPONSE" | python3 -c "
import json, sys, re
try:
    resp = json.load(sys.stdin)
    text = resp.get('content', [{}])[0].get('text', '')
    # Try to find a JSON object with 'intent' key (may have markdown wrapping)
    match = re.search(r'\{[^{}]*\"intent\"[^{}]*\}', text, re.DOTALL)
    if match:
        data = json.loads(match.group())
        print(json.dumps(data))
    else:
        # Try parsing the whole text as JSON
        data = json.loads(text)
        print(json.dumps(data))
except Exception:
    print('{}')
" 2>/dev/null || echo "{}")

        if [ "$EXTRACTED" != "{}" ]; then
            INTENT=$(echo "$EXTRACTED" | python3 -c "import json,sys; print(json.load(sys.stdin).get('intent',''))" 2>/dev/null || echo "")
            DECISIONS=$(echo "$EXTRACTED" | python3 -c "import json,sys; print(json.dumps(json.load(sys.stdin).get('decisions',[])))" 2>/dev/null || echo "[]")
            FILES_MODIFIED=$(echo "$EXTRACTED" | python3 -c "import json,sys; print(json.dumps(json.load(sys.stdin).get('files_modified',[])))" 2>/dev/null || echo "[]")
            NEXT_STEPS=$(echo "$EXTRACTED" | python3 -c "import json,sys; print(json.dumps(json.load(sys.stdin).get('next_steps',[])))" 2>/dev/null || echo "[]")
            LESSONS=$(echo "$EXTRACTED" | python3 -c "import json,sys; print(json.dumps(json.load(sys.stdin).get('lessons',[])))" 2>/dev/null || echo "[]")
        fi
    fi
fi

# ── PII scan on extracted content ──────────────────────────────────
if [ -n "$INTENT" ] && [ -f "$PII_PATTERNS" ]; then
    # Escape intent for safe Python embedding
    ESCAPED_INTENT=$(echo "$INTENT" | python3 -c "import sys,json; print(json.dumps(sys.stdin.read()))" 2>/dev/null || echo '""')

    HAS_PII=$(python3 -c "
import json, re
patterns = json.load(open('$PII_PATTERNS'))
text = json.loads($ESCAPED_INTENT)
for p in patterns.get('patterns', []):
    if p.get('action') == 'block' and re.search(p['pattern'], text):
        print('BLOCKED:' + p['id'])
        break
else:
    print('CLEAN')
" 2>/dev/null || echo "CLEAN")

    if [[ "$HAS_PII" == BLOCKED* ]]; then
        BLOCKED_PATTERN="${HAS_PII#BLOCKED:}"
        INTENT="[PII blocked - session content contained sensitive data]"
        DECISIONS="[]"
        NEXT_STEPS="[]"
        LESSONS="[]"
        # Log the block
        python3 -c "
import json
log = []
try: log = json.load(open('.claude/learning/pii-blocked.json'))
except: pass
log.append({
    'timestamp': '$(date -u +%Y-%m-%dT%H:%M:%SZ)',
    'pattern_id': '$BLOCKED_PATTERN',
    'source': 'checkpoint-extraction'
})
json.dump(log, open('.claude/learning/pii-blocked.json', 'w'), indent=2)
" 2>/dev/null || true
    fi
fi

# ── Write checkpoint ───────────────────────────────────────────────
TIMESTAMP=$(date -u +%Y-%m-%dT%H:%M:%SZ)
SAFE_TIMESTAMP=$(echo "$TIMESTAMP" | tr ':.' '-')

# Escape intent for safe JSON embedding
ESCAPED_INTENT_JSON=$(echo "$INTENT" | python3 -c "import sys,json; print(json.dumps(sys.stdin.read().strip()))" 2>/dev/null || echo '""')

python3 -c "
import json

checkpoint = {
    'schema_version': '1.0.0',
    'timestamp': '$TIMESTAMP',
    'project': $(echo "$PROJECT_NAME" | python3 -c "import sys,json; print(json.dumps(sys.stdin.read().strip()))"),
    'session_number': $SESSION_COUNT,
    'intent': json.loads($ESCAPED_INTENT_JSON),
    'decisions': $DECISIONS,
    'files_modified': $FILES_MODIFIED,
    'next_steps': $NEXT_STEPS,
    'lessons': $LESSONS,
    'git_status': {
        'branch': '$GIT_BRANCH',
        'latest_commit': '$GIT_COMMIT',
        'dirty_files': $GIT_DIRTY
    },
    'files_changed_since_last': $GIT_FILES_CHANGED
}

json.dump(checkpoint, open('$LATEST', 'w'), indent=2)
json.dump(checkpoint, open('$CHECKPOINT_DIR/checkpoint_${SAFE_TIMESTAMP}.json', 'w'), indent=2)
" 2>/dev/null || true

exit 0
