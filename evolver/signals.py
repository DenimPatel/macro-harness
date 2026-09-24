"""Labels on turns, read from what the user and the harness already did.

A personal harness has no benchmark, but it has a history, and the history is
full of verdicts: the user said "no, actually", the user asked for the same
thing they asked for last week, the user approved the same command for the
twentieth time, the same error came back, the turn ran out of steps. Each of
these is a cheap, honest signal. None of them needs a model to compute.
"""

import difflib
import re

CORRECTION = re.compile(
    r"^\s*(no\b|nope\b|actually\b|that'?s (wrong|not)|wrong\b|undo\b|revert\b|"
    r"that is (wrong|not)|stop\b|don'?t\b)", re.IGNORECASE)
RESTATEMENT_RATIO = 0.85
RESTATEMENT_MIN_CHARS = 15
USER_ALLOWED = ("allowed once by the user", "always allowed")
USER_DENIED = ("denied by the user",)


def answer_from_note(note):
    """What the user answered, reconstructed from the tool event's note."""
    note = note or ""
    if note.startswith("always allowed"):
        return "a"
    if note.startswith("allowed once by the user"):
        return "y"
    if note.startswith("denied by the user"):
        return "d"
    return None


def corrections(episodes):
    """Turns the user pushed back on in the very next message."""
    found = []
    for episode in episodes:
        for turn, following in zip(episode.turns, episode.turns[1:]):
            if CORRECTION.match(following.user_text or ""):
                found.append(turn.ref)
    return found


def restatements(episodes):
    """Clusters of near-identical user messages that span more than one session."""
    texts = [(turn.ref, turn.user_text.strip()) for episode in episodes
             for turn in episode.turns
             if len(turn.user_text.strip()) >= RESTATEMENT_MIN_CHARS]
    clusters = []
    for ref, text in texts:
        for cluster in clusters:
            if difflib.SequenceMatcher(None, cluster["text"].lower(),
                                       text.lower()).ratio() >= RESTATEMENT_RATIO:
                cluster["refs"].append(ref)
                break
        else:
            clusters.append({"text": text, "refs": [ref]})
    return [cluster for cluster in clusters
            if len({ref["session"] for ref in cluster["refs"]}) >= 2]


def asks(episodes):
    """Every permission prompt the user answered, with the call's full arguments."""
    found = []
    for episode in episodes:
        for turn in episode.turns:
            for event in turn.tool_events:
                if event.get("decision") != "ask":
                    continue
                found.append({
                    "ref": turn.ref, "turn": turn, "tool": event.get("name"),
                    "arg": event.get("arg") or "",
                    "arguments": turn.arguments_for(event.get("tool_call_id")),
                    "answer": answer_from_note(event.get("note")),
                })
    return found


def error_signature(content):
    """The first line of an error with numbers and paths blurred, so repeats match."""
    text = (content or "").strip()
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith(("exit ", "stdout:", "stderr:")):
            text = line
            break
    text = re.sub(r"(/[\w.\-]+)+", "<path>", text)
    text = re.sub(r"\d+", "N", text)
    return text[:120]


def errors(episodes):
    found = []
    for episode in episodes:
        for turn in episode.turns:
            for event in turn.tool_events:
                failed = event.get("outcome") == "error"
                content = event.get("content") or ""
                if not failed and content.startswith("exit ") and not content.startswith("exit 0"):
                    failed = True
                if failed:
                    found.append({"ref": turn.ref, "tool": event.get("name"),
                                  "signature": error_signature(content)})
    return found


def stops(episodes):
    return [{"ref": turn.ref, "stopped": turn.stopped}
            for episode in episodes for turn in episode.turns
            if turn.stopped in ("max_steps", "budget")]


def retries(episodes):
    return [{"ref": turn.ref, "status": retry.get("status"), "attempt": retry.get("attempt")}
            for episode in episodes for turn in episode.turns for retry in turn.retries]


def metrics(episodes):
    """The numbers that say whether the harness is getting better for this user."""
    turns = [turn for episode in episodes for turn in episode.turns]
    completed = [turn for turn in turns if turn.stopped == "none" and turn.end]
    ask_list = asks(episodes)
    correction_list = corrections(episodes)
    restated = restatements(episodes)
    tool_events = [event for turn in turns for event in turn.tool_events]
    error_list = errors(episodes)

    def rate(numerator, denominator):
        return round(numerator / denominator, 4) if denominator else 0.0

    return {
        "sessions": len(episodes),
        "turns": len(turns),
        "tool_calls": len(tool_events),
        "asks": len(ask_list),
        "asks_per_session": rate(len(ask_list), len(episodes)),
        "denied_by_user": sum(1 for a in ask_list if a["answer"] == "d"),
        "correction_rate": rate(len(correction_list), len(turns)),
        "restatement_clusters": len(restated),
        "tool_error_rate": rate(len(error_list), len(tool_events)),
        "stops": len(stops(episodes)),
        "retries": len(retries(episodes)),
        "tokens_per_completed_turn": rate(sum(t.tokens for t in completed), len(completed)),
        "tainted_turns": sum(1 for turn in turns if turn.tainted),
    }
