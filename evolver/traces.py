"""Session logs, read-only, turned into episodes.

An episode is one session log. A turn is everything from one user message to
the next: the assistant messages the model produced, the tool events the loop
recorded, and how the turn ended. Replay needs the assistant messages verbatim;
the miners and metrics need the tool events and the turn ends.

Nothing here writes to the workspace. The log is the harness's source of truth,
and the evolver only ever reads it.
"""

import json
import os
import re

from .adapters import macroharness as adapter

NETWORK = re.compile(r"\b(curl|wget|nc|ssh|scp|rsync|ftp)\b|https?://")


class Turn:
    def __init__(self, session_id, index, user_text, header):
        self.session_id = session_id
        self.index = index
        self.user_text = user_text
        self.header = header or {}
        self.assistant_messages = []
        self.tool_events = []
        self.end = None
        self.retries = []
        self.uses = []
        self.compactions = 0

    @property
    def ref(self):
        return {"session": self.session_id, "turn": self.index}

    @property
    def stopped(self):
        return (self.end or {}).get("stopped") or "none"

    @property
    def tokens(self):
        return int((self.end or {}).get("tokens") or 0)

    def arguments_for(self, tool_call_id):
        """The arguments the model sent for one call, from its assistant message."""
        for message in self.assistant_messages:
            for call in message.get("tool_calls") or ():
                if call.get("id") == tool_call_id:
                    raw = (call.get("function") or {}).get("arguments")
                    if isinstance(raw, dict):
                        return raw
                    try:
                        value = json.loads(raw or "{}")
                    except ValueError:
                        return {}
                    return value if isinstance(value, dict) else {}
        return {}

    @property
    def tainted(self):
        """True when content the user did not write entered this turn's context.

        MCP tools (`server__tool`) and network commands bring in text from
        outside. A proposal mined from such a turn may be the echo of an
        injection, so it is labelled and never accepted in bulk.
        """
        for event in self.tool_events:
            name = event.get("name") or ""
            if "__" in name:
                return True
            if name == "run_bash" and NETWORK.search(event.get("arg") or ""):
                return True
        return False

    def summary(self):
        return {"ref": self.ref, "user": self.user_text[:200], "stopped": self.stopped,
                "tokens": self.tokens, "tools": len(self.tool_events),
                "tainted": self.tainted}


class Episode:
    def __init__(self, session_id, path, events):
        self.id = session_id
        self.path = path
        self.events = events
        self.headers = [e for e in events if e.get("t") == "header"]
        self.turns = []
        self._build()

    @property
    def header(self):
        return self.headers[0] if self.headers else {}

    @property
    def model(self):
        return self.header.get("model")

    def _build(self):
        header = None
        turn = None
        for event in self.events:
            kind = event.get("t")
            if kind == "header":
                header = event
            elif kind == "user":
                turn = Turn(self.id, len(self.turns), event.get("text") or "", header)
                self.turns.append(turn)
            elif turn is None:
                continue
            elif kind == "assistant":
                turn.assistant_messages.append(event.get("message") or {})
            elif kind == "tool":
                turn.tool_events.append(event)
            elif kind == "turn_end":
                turn.end = event
            elif kind == "retry":
                turn.retries.append(event)
            elif kind == "artifact_use":
                turn.uses.append(event.get("id"))
            elif kind == "compact":
                turn.compactions += 1

    def uses(self):
        return [use for turn in self.turns for use in turn.uses]


def load(workspace):
    """Every main session log in the workspace, oldest first."""
    episodes = []
    for path in adapter.session_files(workspace):
        session_id = os.path.splitext(os.path.basename(path))[0]
        episodes.append(Episode(session_id, path, adapter.read_events(path)))
    return episodes


def by_id(episodes):
    return {episode.id: episode for episode in episodes}
