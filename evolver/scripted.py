"""A model that says exactly what the recording says it said.

Tier 1 replay feeds each turn's recorded assistant messages back into a real
harness, one per model call. The harness does everything else for real --
permissions, hooks, tools, containment -- so any difference between a baseline
run and a candidate run is caused by the candidate, not by the model.

When a candidate lets a turn run longer than the recording did (a higher step
limit, say), the recording runs out; the model then ends the turn with a marker
instead of inventing anything.
"""

END_OF_RECORDING = "[replay: the recording ends here]"


class Completion:
    def __init__(self, message, usage=None):
        self.message = message
        self.usage = usage or {}


class ScriptedModel:
    def __init__(self, replies=()):
        self.replies = []
        self.calls = 0
        self.ran_out = 0
        self.load(replies)

    def load(self, replies):
        """Queue one turn's recorded assistant messages, dropping any leftovers."""
        self.replies = [dict(message) for message in replies]

    def complete(self, messages, tools, on_text=None):
        self.calls += 1
        if not self.replies:
            self.ran_out += 1
            return Completion({"role": "assistant", "content": END_OF_RECORDING})
        message = self.replies.pop(0)
        message.pop("usage", None)
        if on_text is not None and message.get("content"):
            on_text(message["content"])
        return Completion(message)
