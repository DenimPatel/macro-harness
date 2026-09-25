"""Is it working? Numbers per window, and per artifact.

The metrics are the review's "how you'll know": correction and restatement rates
should fall, prompts per session should fall, and the error rate and tokens per
completed turn should not rise. Per artifact, activation is the honest measure
of whether a change is earning its context: an applied rule that never matches
is cost with no benefit.
"""

from .signals import metrics


def windows(episodes, size=20):
    recent = episodes[-size:]
    previous = episodes[-2 * size:-size]
    return {"recent": metrics(recent), "previous": metrics(previous) if previous else None}


def artifacts(episodes, active):
    """Activations of every artifact in force, from `artifact_use` events."""
    rows = []
    for artifact_id, entry in sorted(active.items()):
        after = [e for e in episodes if e.id > (entry.get("applied_at") or "")]
        used_in = [e.id for e in after if artifact_id in e.uses()]
        uses = sum(e.uses().count(artifact_id) for e in after)
        rows.append({
            "id": artifact_id, "kind": entry.get("kind"), "uses": uses,
            "sessions_since_applied": len(after),
            "sessions_since_last_use": (len(after) - 1 - [e.id for e in after].index(used_in[-1]))
            if used_in else len(after),
            "model": entry.get("model"),
        })
    return rows


def report(episodes, active, size=20):
    lines = []
    window = windows(episodes, size)
    recent, previous = window["recent"], window["previous"]
    lines.append("last %d session(s):" % min(size, len(episodes)))
    for key in ("sessions", "turns", "asks_per_session", "correction_rate",
                "restatement_clusters", "tool_error_rate", "stops", "retries",
                "tokens_per_completed_turn", "tainted_turns"):
        value = recent[key]
        if previous is not None:
            lines.append("  %-26s %-10s (previous %s)" % (key, value, previous[key]))
        else:
            lines.append("  %-26s %s" % (key, value))
    rows = artifacts(episodes, active)
    if rows:
        lines.append("artifacts in force:")
        for row in rows:
            lines.append("  %-32s %-12s uses %-4d sessions since applied %-4d since last use %d"
                         % (row["id"], row["kind"], row["uses"], row["sessions_since_applied"],
                            row["sessions_since_last_use"]))
    else:
        lines.append("no evolver artifacts in force")
    return "\n".join(lines)
