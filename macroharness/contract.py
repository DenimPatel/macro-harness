"""The data shapes the harness writes and an outside process may read.

The harness does its work and leaves a trace; an evolver, run later and
elsewhere, reads that trace and proposes changes. The two share nothing but the
shapes below. Keeping them in one small module is what lets the evolver be
replaced -- or pointed at a different harness -- without touching the loop.

Nothing here does I/O except the two small readers at the bottom, and nothing
here changes what the harness does. It only names things.
"""

import hashlib
import json
import os

TRACE_VERSION = 1

# --- trace events (the "t" field of a session JSONL line) ---------------------
# Pre-existing kinds, replayed into messages by session.messages_from_events:
SYSTEM, USER, ASSISTANT, TOOL, COMPACT, BUDGET = (
    "system", "user", "assistant", "tool", "compact", "budget")
# Kinds added for the evolver. messages_from_events ignores them, so they never
# change what a resumed session rebuilds.
HEADER = "header"          # once, first line: what harness state this ran under
TURN_END = "turn_end"      # steps, tokens, stopped: none | max_steps | budget
RETRY = "retry"            # a model call that was retried: status, attempt
ARTIFACT_USE = "artifact_use"  # an evolver-applied artifact matched or ran: id

# Outcome of one tool call, as written on the TOOL event.
OK, ERROR, DENIED, BLOCKED = "ok", "error", "denied", "blocked"

# How a turn ended.
STOPPED_NONE, STOPPED_MAX_STEPS, STOPPED_BUDGET = "none", "max_steps", "budget"

# --- artifacts ------------------------------------------------------------------
POLICY_RULE, HOOK, TOOL_DEF, STRATEGY, FACT, RETIRE = (
    "policy_rule", "hook", "tool", "strategy", "fact", "retire")
ARTIFACT_KINDS = (POLICY_RULE, HOOK, TOOL_DEF, STRATEGY, FACT, RETIRE)

# The workspace-side index an evolver writes after applying something. It only
# labels artifacts for logging and counts the inbox; nothing in it changes
# behavior, so a tampered copy can at worst produce wrong statistics.
EVOLVED_INDEX = "evolved.json"

# Every ledger entry carries these keys. `action` is apply | retire | revert;
# `undo` is what revert replays to take the entry back out.
LEDGER_FIELDS = ("id", "action", "kind", "payload", "target", "digest", "model",
                 "applied_at", "expires_if", "provenance", "undo")


def digest(value):
    """A stable sha256 of a JSON-able value or of bytes."""
    if not isinstance(value, bytes):
        value = json.dumps(value, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def hook_key(hook):
    """What identifies a hook for accounting: where it fires and what it runs."""
    return [hook.get("event"), hook.get("tool") or "*", hook.get("arg") or "*",
            hook.get("run")]


def rule_key(rule):
    return [rule.get("tool"), rule.get("arg"), rule.get("verb")]


def evolved_index_path(state_dir):
    return os.path.join(state_dir, EVOLVED_INDEX)


def read_evolved_index(state_dir):
    """{"artifacts": {id: {"kind", "match"}}, "inbox_pending": n}. Missing is empty."""
    try:
        with open(evolved_index_path(state_dir), "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    artifacts = data.get("artifacts")
    return {"artifacts": artifacts if isinstance(artifacts, dict) else {},
            "inbox_pending": int(data.get("inbox_pending") or 0)}


def write_evolved_index(state_dir, index):
    path = evolved_index_path(state_dir)
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(index, handle, indent=2, sort_keys=True)
        handle.write("\n")
    os.replace(temporary, path)
