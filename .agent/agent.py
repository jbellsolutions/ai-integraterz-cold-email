#!/usr/bin/env python3
"""
Level 2 Persistent Agent — cold-email-2
Repo: /Users/home/Desktop/AI INtegraterz GTM/Cold Email 2.0

Standalone agent using the Anthropic SDK. Maintains cross-session memory,
proactive scheduling, and conversation history.

Run: python agent.py
Exit: type 'quit' or press Ctrl+C
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import anthropic
import copy

# ── Configuration ─────────────────────────────────────────────────────────────

REPO_NAME = "cold-email-2"
REPO_PATH = Path("/Users/home/Desktop/AI INtegraterz GTM/Cold Email 2.0")
AGENT_DIR = Path(__file__).parent
STATE_FILE = AGENT_DIR / "state.json"
TASKS_FILE = AGENT_DIR / "tasks.json"
IDENTITY_FILE = AGENT_DIR / "identity.json"

MODEL = "claude-opus-4-5"
MAX_TOKENS = 4096
MAX_HISTORY_TURNS = 20  # Keep last N turns in memory to avoid context overflow


# ── Prompt Caching ────────────────────────────────────────────────────────────

def _add_cache_control(messages: list) -> list:
    """Add ephemeral cache_control to the last 3 messages (Anthropic prompt caching).

    Reduces API costs on long sessions by caching recent context.
    Ported from Stanford IRIS Lab meta-harness (TerminalBench 2.0, 76.4%):
    github.com/stanford-iris-lab/meta-harness-tbench2-artifact
    """
    cached = copy.deepcopy(messages)
    for i, msg in enumerate(cached):
        if i < len(cached) - 3:
            continue
        content = msg.get("content")
        if isinstance(content, str):
            msg["content"] = [{"type": "text", "text": content, "cache_control": {"type": "ephemeral"}}]
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and "type" in block:
                    block["cache_control"] = {"type": "ephemeral"}
    return cached

# ── State Management ──────────────────────────────────────────────────────────

EMPTY_STATE = {
    "schema_version": "1.0.0",
    "session_count": 0,
    "last_session": None,
    "last_genome_sync": None,
    "last_learn_cycle": None,
    "last_heal_check": None,
    "conversation_summary": "",
    "pending_tasks": [],
    "notes": [],
}


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE) as f:
                data = json.load(f)
            # Merge with empty state to handle schema additions
            merged = {**EMPTY_STATE, **data}
            return merged
        except (json.JSONDecodeError, OSError) as e:
            print(f"[warn] state.json unreadable ({e}), starting fresh")
    return dict(EMPTY_STATE)


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def load_tasks() -> list:
    if TASKS_FILE.exists():
        try:
            with open(TASKS_FILE) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return []
    return []


def save_tasks(tasks: list) -> None:
    with open(TASKS_FILE, "w") as f:
        json.dump(tasks, f, indent=2)


# ── Context Loaders ───────────────────────────────────────────────────────────

def load_progress() -> str:
    progress_file = REPO_PATH / "claude-progress.txt"
    if not progress_file.exists():
        return "No claude-progress.txt found in repo."
    try:
        text = progress_file.read_text()
        # Return last 40 lines — most recent session context
        lines = text.strip().splitlines()
        return "\n".join(lines[-40:]) if len(lines) > 40 else text
    except OSError as e:
        return f"Could not read claude-progress.txt: {e}"


def load_healing_summary() -> str:
    history_file = REPO_PATH / ".claude" / "healing" / "history.json"
    if not history_file.exists():
        return "No healing history found."
    try:
        with open(history_file) as f:
            history = json.load(f)
        if not history:
            return "Healing history is empty."
        total = len(history)
        last = history[-1]
        error = last.get("error", "unknown error")
        result = last.get("result", "unknown")
        ts = last.get("timestamp", "unknown date")
        return f"{total} total fixes. Last: '{error}' → {result} ({ts})"
    except (json.JSONDecodeError, OSError, KeyError) as e:
        return f"Could not parse healing history: {e}"


def load_observations_count() -> str:
    obs_file = REPO_PATH / ".claude" / "learning" / "observations.json"
    if not obs_file.exists():
        return "No observations file found."
    try:
        with open(obs_file) as f:
            data = json.load(f)
        observations = data.get("observations", data if isinstance(data, list) else [])
        pending = [o for o in observations if o.get("status") == "pending"]
        return f"{len(pending)} pending observations (of {len(observations)} total)"
    except (json.JSONDecodeError, OSError) as e:
        return f"Could not parse observations: {e}"


def load_identity() -> dict | None:
    if IDENTITY_FILE.exists():
        try:
            with open(IDENTITY_FILE) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            print(f"[warn] identity.json unreadable ({e}), using generic prompt")
    return None


def load_typed_memory(repo_path: str) -> str:
    """Load all four typed memory files."""
    memory_dir = os.path.join(repo_path, ".claude", "memory")
    types = [
        ("user", "About You"),
        ("feedback", "Previous Corrections"),
        ("project", "Active Context"),
        ("reference", "Key References"),
    ]
    sections = []
    for mem_type, label in types:
        path = os.path.join(memory_dir, f"{mem_type}.md")
        try:
            content = open(path).read().strip()
            # Skip if only template comments/frontmatter
            lines = [l for l in content.split('\n') if not l.startswith('--') and not l.startswith('<!--') and l.strip()]
            if lines:
                sections.append(f"### {label}\n" + '\n'.join(lines))
        except FileNotFoundError:
            pass
    return '\n\n'.join(sections) if sections else ""


def load_latest_checkpoint(repo_path: str) -> str:
    """Load the latest session checkpoint for 'Previously on...' briefing."""
    checkpoint_path = os.path.join(repo_path, ".claude", "checkpoints", "latest.json")
    try:
        data = json.load(open(checkpoint_path))
        if not data.get("intent"):
            return ""  # Skeleton only, no useful content

        parts = [f"Previously on {data.get('project', 'this project')} (session #{data.get('session_number', '?')}):"]
        if data.get("intent"):
            parts.append(f"Intent: {data['intent']}")
        if data.get("decisions"):
            parts.append("Decisions: " + "; ".join(data["decisions"]))
        if data.get("next_steps"):
            parts.append("Next steps: " + "; ".join(data["next_steps"]))
        if data.get("lessons"):
            parts.append("Lessons: " + "; ".join(data["lessons"]))

        git = data.get("git_status", {})
        if git.get("branch"):
            parts.append(f"Git: {git['branch']} @ {git.get('latest_commit', '?')}, {git.get('dirty_files', 0)} dirty files")

        return "\n".join(parts)
    except (FileNotFoundError, json.JSONDecodeError):
        return ""


def load_genome_summary() -> str:
    genome_file = Path.home() / ".claude" / "agi-1-genome" / "genome.json"
    if not genome_file.exists():
        return "Genome not initialized."
    try:
        with open(genome_file) as f:
            genome = json.load(f)
        version = genome.get("version", "unknown")
        patterns = len(genome.get("healing_patterns", []))
        instructions = len(genome.get("instruction_improvements", []))
        last_updated = genome.get("last_updated", "unknown")
        return f"v{version} | {patterns} patterns | {instructions} instruction improvements | updated {last_updated}"
    except (json.JSONDecodeError, OSError) as e:
        return f"Could not read genome: {e}"


# ── Scheduler ─────────────────────────────────────────────────────────────────

def days_since(iso_date_str: str | None) -> int | None:
    """Return days since the given ISO date string, or None if not set."""
    if not iso_date_str:
        return None
    try:
        dt = datetime.fromisoformat(iso_date_str.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        return (now - dt).days
    except (ValueError, TypeError):
        return None


def check_scheduled_actions(state: dict) -> list[str]:
    """Check which scheduled actions are due. Returns list of reminder messages."""
    reminders = []

    # Weekly: suggest /agi-heal
    heal_days = days_since(state.get("last_heal_check"))
    if heal_days is None or heal_days >= 7:
        days_label = f"{heal_days} days" if heal_days is not None else "never"
        reminders.append(
            f"SCHEDULED: It has been {days_label} since the last heal check. "
            f"Consider running /agi-heal to check for recurring error patterns."
        )

    # Every 10 sessions: suggest /agi-learn
    session_count = state.get("session_count", 0)
    if session_count > 0 and session_count % 10 == 0:
        reminders.append(
            f"SCHEDULED: Session {session_count} reached. "
            f"Run /agi-learn to process accumulated observations and extract insights."
        )

    # Monthly: suggest /agi-sync
    sync_days = days_since(state.get("last_genome_sync"))
    if sync_days is None or sync_days >= 30:
        days_label = f"{sync_days} days" if sync_days is not None else "never"
        reminders.append(
            f"SCHEDULED: It has been {days_label} since the last genome sync. "
            f"Run /agi-sync to push learnings to genome and pull new patterns."
        )

    return reminders


# ── System Prompt Builder ──────────────────────────────────────────────────────

def build_system_prompt(state: dict) -> str:
    progress = load_progress()
    healing = load_healing_summary()
    observations = load_observations_count()
    genome = load_genome_summary()
    memory = load_typed_memory(str(REPO_PATH))
    checkpoint_content = load_latest_checkpoint(str(REPO_PATH))

    session_count = state.get("session_count", 0)
    last_session = state.get("last_session", "Never")
    conversation_summary = state.get("conversation_summary", "")

    identity = load_identity()
    if identity:
        project_name = identity.get("project_name", REPO_NAME)
        description = identity.get("description", "")
        key_components = identity.get("key_components", [])
        language = identity.get("language", "")
        fragile_areas = identity.get("fragile_areas", [])
        entry_points = identity.get("entry_points", [])
        repo_path = identity.get("repo_path", str(REPO_PATH))

        components_str = "\n".join(f"  - {c}" for c in key_components) if key_components else "  (none listed)"
        fragile_str = "\n".join(f"  - {f}" for f in fragile_areas) if fragile_areas else "  (none listed)"
        entry_str = "\n".join(f"  - {e}" for e in entry_points) if entry_points else "  (none listed)"

        identity_block = f"""You are the orchestrator for {project_name}. {description}

Key components:
{components_str}
Language: {language}
Known fragile areas:
{fragile_str}
Entry points:
{entry_str}
Repo path: {repo_path}

You maintain full context for this project across sessions. You know its history, current state, and what needs doing. When the user asks about the project, you answer with authority. When changes are needed, you coordinate them."""
    else:
        identity_block = f"You are the Level 2 persistent agent for the repo **{REPO_NAME}** at `{REPO_PATH}`."

    memory_section = (
        f"\nRepo Memory (learned facts about this specific repo):\n{memory}\n"
        if memory else ""
    )

    checkpoint_section = (
        f"\nLast Session:\n{checkpoint_content}\n"
        if checkpoint_content else ""
    )

    system = f"""{identity_block}{memory_section}{checkpoint_section}
You maintain cross-session memory and proactive scheduling for this repo. You know AGI-1's
sub-commands: /agi-1, /agi-heal, /agi-learn, /agi-sync, /agi-audit, /agi-tdd, /agi-debug, /agi-verify.

## Current Repo State

**claude-progress.txt (last 40 lines):**
{progress}

**Healing:** {healing}

**Learning:** {observations}

**Genome:** {genome}

## Session Memory

Session count: {session_count}
Last session: {last_session}
{f"Previous session summary: {conversation_summary}" if conversation_summary else "No previous session summary."}

## Your Role

- Answer questions about this repo directly and concisely
- Recommend the right AGI-1 sub-command when the user describes a problem
- Track what was accomplished and update state when asked
- Surface scheduled reminders at session start
- Be direct. No filler. No "I'll" or "Let me" — just do it.

When the user says "done", "exit", or "quit", summarize the session in 2-3 sentences
and say "Type 'quit' to exit and save state."
"""
    return system


# ── Conversation History ───────────────────────────────────────────────────────

def trim_history(history: list) -> list:
    """Keep only the last MAX_HISTORY_TURNS turns (pairs of user+assistant)."""
    if len(history) > MAX_HISTORY_TURNS * 2:
        return history[-(MAX_HISTORY_TURNS * 2):]
    return history


# ── Main Loop ─────────────────────────────────────────────────────────────────

def run_agent() -> None:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("ERROR: ANTHROPIC_API_KEY not set.")
        print("Set it with: export ANTHROPIC_API_KEY='sk-ant-...'")
        sys.exit(1)

    client = anthropic.Anthropic(api_key=api_key)
    state = load_state()

    # Increment session count
    state["session_count"] = state.get("session_count", 0) + 1
    now_iso = datetime.now(timezone.utc).isoformat()

    print(f"\n{REPO_NAME} — Level 2 Agent")
    print(f"Session #{state['session_count']} | {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("─" * 50)

    # Check scheduled actions
    reminders = check_scheduled_actions(state)
    if reminders:
        print("\nScheduled reminders:")
        for r in reminders:
            print(f"  {r}")
        print()

    print("Type your message. Enter 'quit' to exit and save.\n")

    # Build system prompt with fresh context
    system_prompt = build_system_prompt(state)
    conversation: list[dict] = []

    # Initial greeting from agent
    try:
        greeting_messages = [{"role": "user", "content": "Start the session with a one-paragraph repo brief."}]
        greeting_response = client.messages.create(
            model=MODEL,
            max_tokens=512,
            system=system_prompt,
            messages=_add_cache_control(greeting_messages),
        )
        greeting_text = greeting_response.content[0].text
        print(f"Agent: {greeting_text}\n")
        # Don't add the artificial greeting prompt to conversation history
    except anthropic.APIError as e:
        print(f"[warn] Could not generate greeting: {e}")
        print()

    # Main chat loop
    while True:
        try:
            user_input = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n\n[Interrupted] Saving state...")
            break

        if not user_input:
            continue

        if user_input.lower() in ("quit", "exit", "q"):
            print("\nSaving state and exiting...")
            break

        conversation.append({"role": "user", "content": user_input})
        conversation = trim_history(conversation)

        try:
            response = client.messages.create(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                system=system_prompt,
                messages=_add_cache_control(conversation),
            )
            assistant_text = response.content[0].text
            conversation.append({"role": "assistant", "content": assistant_text})
            print(f"\nAgent: {assistant_text}\n")

        except anthropic.RateLimitError:
            print("[error] Rate limit hit. Wait a moment and try again.\n")
            conversation.pop()  # Remove the user message that failed
        except anthropic.APIStatusError as e:
            print(f"[error] API error {e.status_code}: {e.message}\n")
            conversation.pop()
        except anthropic.APIConnectionError:
            print("[error] Connection failed. Check your internet connection.\n")
            conversation.pop()

    # ── Save state on exit ──────────────────────────────────────────────────
    state["last_session"] = now_iso

    # Generate a brief session summary from conversation
    if len(conversation) >= 2:
        try:
            summary_response = client.messages.create(
                model=MODEL,
                max_tokens=150,
                system="Summarize the following conversation in 2 sentences. Be concise.",
                messages=conversation[-6:],  # Last 3 turns
            )
            state["conversation_summary"] = summary_response.content[0].text
        except anthropic.APIError:
            # Non-fatal — just skip the summary
            pass

    save_state(state)
    print(f"State saved. Session #{state['session_count']} complete.")
    print(f"Genome: {load_genome_summary()}")


if __name__ == "__main__":
    run_agent()
