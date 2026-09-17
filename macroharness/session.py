"""The session log: an append-only JSONL file, and the replay that reads it.

The log is the source of truth. The model's message list is *derived* by
replaying events, which is what makes --resume and compaction agree: resuming
does not restore a snapshot, it re-runs the same fold the live session ran.
"""

import json
import os
import time


def slugify(text, limit=32):
    """Turn a first prompt into something usable in a filename."""
    kept = []
    for character in text.lower():
        if character.isalnum():
            kept.append(character)
        elif kept and kept[-1] != "-":
            kept.append("-")
    return "".join(kept).strip("-")[:limit]


class Session:
    """One append-only JSONL file. Writing is the only mutation."""

    def __init__(self, path):
        self.path = path
        self.id = os.path.splitext(os.path.basename(path))[0]

    @classmethod
    def create(cls, sessions_dir, label=""):
        os.makedirs(sessions_dir, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        suffix = slugify(label) or "session"
        return cls(os.path.join(sessions_dir, "%s-%s.jsonl" % (stamp, suffix)))

    @classmethod
    def at(cls, sessions_dir, name):
        return cls(os.path.join(sessions_dir, name + ".jsonl"))

    @classmethod
    def resume(cls, sessions_dir, session_id):
        """session_id is a file stem, or 'latest' for the newest log."""
        if session_id in (None, "latest"):
            candidates = sorted(
                entry for entry in os.listdir(sessions_dir)
                if entry.endswith(".jsonl") and ".sub-" not in entry
            ) if os.path.isdir(sessions_dir) else []
            if not candidates:
                raise FileNotFoundError("no sessions in %s" % sessions_dir)
            return cls(os.path.join(sessions_dir, candidates[-1]))
        path = os.path.join(sessions_dir, session_id + ".jsonl")
        if not os.path.exists(path):
            raise FileNotFoundError("no session %r in %s" % (session_id, sessions_dir))
        return cls(path)

    def append(self, event):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")

    def events(self):
        events = []
        if not os.path.exists(self.path):
            return events
        with open(self.path, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue  # a truncated tail line is not worth dying over
        return events

    def messages(self):
        return messages_from_events(self.events())


def messages_from_events(events):
    """Rebuild the model's message list from the log.

    A compact event does not delete anything from the log; it records which live
    indexes were folded and what replaced them, so replaying the log in order
    reproduces exactly the list the process held.
    """
    messages = []
    for event in events:
        kind = event.get("t")
        if kind == "system":
            messages.append({"role": "system", "content": event.get("text", "")})
        elif kind == "user":
            messages.append({"role": "user", "content": event.get("text", "")})
        elif kind == "assistant":
            messages.append(event["message"])
        elif kind == "tool":
            messages.append({
                "role": "tool",
                "tool_call_id": event.get("tool_call_id", ""),
                "content": event.get("content", ""),
            })
        elif kind == "compact":
            folded = set(event.get("folded") or [])
            if not folded:
                continue
            first = min(folded)
            messages = [message for index, message in enumerate(messages)
                        if index not in folded]
            messages.insert(min(first, len(messages)),
                            {"role": "system", "content": event.get("summary", "")})
    return messages
