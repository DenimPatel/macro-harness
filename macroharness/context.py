"""Token accounting and context compaction.

micro-harness resends the full message list every step and says so. Once you
resend everything you need two numbers (what did that cost, how close is the
context) and one operation (fold the old part away). Both live here.

Compaction's one real invariant: never split a tool_call from its results. A
naive summarizer breaks the pairing and the next request is rejected, so the
fold boundary is widened until it is safe, and compaction is skipped if no safe
boundary exists.
"""

import json

CHARS_PER_TOKEN = 4
CONTEXT_LIMIT = 64_000
COMPACT_AT = int(CONTEXT_LIMIT * 0.75)
KEEP_RECENT = 6
SUMMARY_MAX_CHARS_PER_MESSAGE = 1500

PRICES = {"deepseek-chat": (0.27, 1.10)}  # USD per million tokens: input, output

SUMMARY_PROMPT = (
    "You are compacting an agent transcript. Write a short handover note that "
    "keeps every decision made, every file created or changed, every command "
    "whose result still matters, every error still unresolved, and the user's "
    "current request. Drop pleasantries and repeated tool output. Write notes, "
    "not prose."
)


def estimate_tokens(messages):
    """A character-count estimate, used when the provider sends no usage."""
    characters = 0
    for message in messages:
        characters += len(message.get("content") or "")
        for call in message.get("tool_calls") or []:
            characters += len(json.dumps(call))
    return (characters + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN


class Accounting:
    """Running totals for one session. Subagents share the parent's object."""

    def __init__(self, model_name=None):
        self.model_name = model_name
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.last_prompt = 0
        self.calls = 0

    @property
    def total(self):
        return self.prompt_tokens + self.completion_tokens

    def add(self, usage, request_messages, reply=None):
        self.calls += 1
        if usage and usage.get("prompt_tokens") is not None:
            self.prompt_tokens += usage.get("prompt_tokens") or 0
            self.completion_tokens += usage.get("completion_tokens") or 0
            self.last_prompt = usage.get("prompt_tokens") or 0
            return
        self.last_prompt = estimate_tokens(request_messages)
        self.prompt_tokens += self.last_prompt
        if reply is not None:
            self.completion_tokens += estimate_tokens([reply])

    def cost(self):
        price = PRICES.get(self.model_name)
        if price is None:
            return None
        return (self.prompt_tokens * price[0] + self.completion_tokens * price[1]) / 1_000_000

    def report(self):
        cost = self.cost()
        lines = [
            "calls: %d" % self.calls,
            "prompt tokens: %d (last request: %d)" % (self.prompt_tokens, self.last_prompt),
            "completion tokens: %d" % self.completion_tokens,
            "total: %d" % self.total,
        ]
        if cost is not None:
            lines.append("estimated cost: $%.4f" % cost)
        return "\n".join(lines)


def fold_boundary(messages, keep_recent=KEEP_RECENT):
    """Index to fold below: messages[1:boundary] go, messages[0] and the rest stay.

    Returns None when nothing can be folded safely — which is the correct
    failure mode, not a reason to fold a tool_call away from its results.
    """
    boundary = len(messages) - keep_recent
    if boundary <= 1:
        return None
    while boundary < len(messages) and messages[boundary].get("role") == "tool":
        boundary += 1
    if boundary >= len(messages):
        return None
    return boundary


def render(messages, per_message=SUMMARY_MAX_CHARS_PER_MESSAGE):
    lines = []
    for message in messages:
        role = message.get("role", "?")
        body = message.get("content") or ""
        if len(body) > per_message:
            body = body[:per_message] + "... [truncated]"
        calls = message.get("tool_calls") or []
        if calls:
            body += "\n" + "\n".join(
                "tool_call %s(%s)" % (call.get("function", {}).get("name"),
                                      call.get("function", {}).get("arguments"))
                for call in calls
            )
        lines.append("%s: %s" % (role, body))
    return "\n\n".join(lines)


def compact(messages, model, accounting, budget=None):
    """Fold the old half of the context into one summary message.

    Returns (messages, event). event is None when nothing safe could be folded,
    and the caller should say so rather than pretending it compacted.
    """
    boundary = fold_boundary(messages)
    if boundary is None:
        return messages, None
    folded = messages[1:boundary]
    before = estimate_tokens(messages)
    request = [
        {"role": "system", "content": SUMMARY_PROMPT},
        {"role": "user", "content": render(folded)},
    ]
    if budget is not None and accounting.total >= budget:
        return messages, None
    completion = model.complete(request, [])
    accounting.add(completion.usage, request, completion.message)
    summary = (completion.message.get("content") or "").strip()
    if not summary:
        return messages, None
    folded_indexes = list(range(1, boundary))
    new_messages = ([messages[0],
                     {"role": "system", "content": "[summary of %d earlier messages] %s"
                      % (len(folded), summary)}]
                    + messages[boundary:])
    event = {
        "t": "compact",
        "folded": folded_indexes,
        "summary": new_messages[1]["content"],
        "tokens_before": before,
        "tokens_after": estimate_tokens(new_messages),
    }
    return new_messages, event
