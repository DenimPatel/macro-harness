"""Control-flow parameters as data: data proposes, code bounds.

A handful of numbers decide how the loop behaves under pressure: how often a
failed model call is retried and on which statuses, how many tool rounds a turn
may take, how wide parallel dispatch goes, when compaction kicks in. They used
to be constants. Here they are read from `.macroharness/strategy.json`, so
something outside the loop -- the user, or an offline evolver -- can tune them
without touching the source.

Moving a decision into a file must not move its bounds with it. Every value is
clamped to a hard cap written below, in code, which no file can change; and the
product of retries and steps is capped too, because two knobs that are each
reasonable can multiply into a runaway spend. The endpoint and the API key are
deliberately not settable here: which host the conversation goes to is a trust
boundary, not a knob.

The file lives in the workspace, so it crosses trust-on-first-use like the
policy and the hooks. An untrusted file is ignored, not fatal.
"""

import json
import os

from . import permissions

TRUST_KEY = "strategy_sha256"

DEFAULTS = {
    "retry": {"max": 3, "backoff_cap": 8, "on_status": None},
    "max_steps": 16,
    "parallel_tools": 4,
    "compact_at_ratio": 0.75,
}

# The bounds. Constants in code, on purpose: a strategy file can move a value
# inside these, never past them.
HARD_CAPS = {
    "retry.max": (0, 6),
    "retry.backoff_cap": (1, 30),
    "max_steps": (1, 64),
    "parallel_tools": (1, 8),
    "compact_at_ratio": (0.3, 0.9),
}
RETRYABLE_STATUSES = frozenset((408, 409, 425, 429, 500, 502, 503, 504, 529))
MAX_RETRY_STEP_PRODUCT = 200  # worst-case model calls per turn from retries x steps
KNOWN_KEYS = ("version", "retry", "max_steps", "parallel_tools", "compact_at_ratio")
RETRY_KEYS = ("max", "backoff_cap", "on_status")


class StrategyError(Exception):
    pass


def strategy_path(state_dir):
    return os.path.join(state_dir, "strategy.json")


def _bounded(name, value, cast):
    low, high = HARD_CAPS[name]
    try:
        number = cast(value)
    except (TypeError, ValueError):
        raise StrategyError("%s must be a number, got %r" % (name, value))
    return min(max(number, low), high), number


def clamp(raw):
    """Turn a strategy object into effective settings. Returns (settings, notes).

    `notes` lists every value that was ignored or pulled back inside a cap, so a
    caller can print them -- and an evolver can refuse a proposal that needed
    clamping at all.
    """
    if not isinstance(raw, dict):
        raise StrategyError("strategy.json must contain a JSON object")
    notes = []
    for key in raw:
        if key not in KNOWN_KEYS:
            notes.append("ignored %r: not a strategy setting" % key)
    retry_raw = raw.get("retry") or {}
    if not isinstance(retry_raw, dict):
        raise StrategyError("retry must be an object")
    for key in retry_raw:
        if key not in RETRY_KEYS:
            notes.append("ignored retry.%s: not a retry setting" % key)

    settings = json.loads(json.dumps(DEFAULTS))
    for name, source, key, cast in (
            ("retry.max", retry_raw, "max", int),
            ("retry.backoff_cap", retry_raw, "backoff_cap", float),
            ("max_steps", raw, "max_steps", int),
            ("parallel_tools", raw, "parallel_tools", int),
            ("compact_at_ratio", raw, "compact_at_ratio", float)):
        if key not in source:
            continue
        value, asked = _bounded(name, source[key], cast)
        if value != asked:
            notes.append("clamped %s from %r to %r" % (name, asked, value))
        if name.startswith("retry."):
            settings["retry"][key] = value
        else:
            settings[key] = value

    statuses = retry_raw.get("on_status")
    if statuses is not None:
        if not isinstance(statuses, list) or not all(isinstance(s, int) for s in statuses):
            raise StrategyError("retry.on_status must be a list of HTTP status codes")
        kept = sorted(set(statuses) & RETRYABLE_STATUSES)
        dropped = sorted(set(statuses) - RETRYABLE_STATUSES)
        if dropped:
            notes.append("ignored retry.on_status %s: never retryable" % dropped)
        settings["retry"]["on_status"] = kept or None

    worst = (settings["retry"]["max"] + 1) * settings["max_steps"]
    if worst > MAX_RETRY_STEP_PRODUCT:
        allowed = max(MAX_RETRY_STEP_PRODUCT // settings["max_steps"] - 1, 0)
        notes.append("clamped retry.max from %d to %d: retries x steps is capped at %d"
                     % (settings["retry"]["max"], allowed, MAX_RETRY_STEP_PRODUCT))
        settings["retry"]["max"] = allowed
    return settings, notes


def read(path):
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def write(path, raw):
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(raw, handle, indent=2, sort_keys=True)
        handle.write("\n")
    os.replace(temporary, path)


def load(state_dir, interactive=True, prompt_fn=None, out=print):
    """Effective settings for this session. No file, bad file or untrusted: defaults."""
    defaults = json.loads(json.dumps(DEFAULTS))
    path = strategy_path(state_dir)
    if not os.path.exists(path):
        return defaults
    try:
        settings, notes = clamp(read(path))
    except (OSError, ValueError, StrategyError) as error:
        out("[strategy] ignoring %s: %s" % (path, error))
        return defaults
    digest = permissions.policy_digest(path)
    if permissions.trusted_digests(state_dir).get(TRUST_KEY) != digest:
        out("strategy: %s sets:" % path)
        out("  %s" % json.dumps(settings, sort_keys=True))
        if not interactive:
            out("[strategy] not trusted and nobody to ask; using defaults")
            return defaults
        if not (prompt_fn or permissions._default_trust_prompt)("trust this strategy?"):
            out("[strategy] not trusted; using defaults")
            return defaults
        permissions.mark_trusted(state_dir, path, key=TRUST_KEY)
    for note in notes:
        out("[strategy] %s" % note)
    return settings
