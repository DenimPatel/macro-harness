"""Things that stopped earning their place: propose taking them out.

A harness that only ever adds is a harness that slowly gets worse. Every tool
costs schema tokens on every request, and every hook costs time on every call.
This miner proposes retiring:

- an evolver-applied rule, hook or tool with no recorded use in the last N
  sessions since it was applied;
- an evolver-applied artifact learned under one model while the recent sessions
  all ran on another -- workarounds for one model are often dead weight on the
  next;
- a tool or tool hook the agent defined for itself mid-session (`define_tool`,
  `define_hook`) that has not been called or matched in the last N sessions.

Retiring is a proposal like any other: gated, reviewed, reversible.
"""

import time

from ..adapters import macroharness as adapter
from ..adapters.macroharness import contract
from ..proposal import make
from ..split import require_mining_set

TOOL_EVENTS = ("pre_tool", "post_tool", "tool_error")


def _stamp(seconds):
    return time.strftime("%Y%m%d-%H%M%S", time.localtime(seconds))


def _proposal(target_kind, target, reason, ledger_id, sessions, effect):
    return make(
        "retire", {"target_kind": target_kind, "target": target, "ledger_id": ledger_id,
                   "reason": reason},
        miner="retire", predicts_fix=[{"session": s, "turn": 0} for s in sessions[-3:]],
        risks=["a rarely needed %s is gone when it is finally needed" % target_kind],
        check="tier 1: no new errors, blocks or denials on held-out sessions",
        evidence=[{"ref": {"session": s, "turn": 0}, "user": "", "note": reason}
                  for s in sessions[-3:]],
        turns=[], effect=effect)


def _hook_matched(hook, episodes):
    for episode in episodes:
        for turn in episode.turns:
            for event in turn.tool_events:
                if adapter.rule_matches(hook, event.get("name") or "", event.get("arg") or ""):
                    return True
    return False


def mine(mining, context):
    require_mining_set(mining)
    unused = context.thresholds["retire_unused_sessions"]
    model_window = context.thresholds["retire_model_sessions"]
    proposals = []
    ledger_hooks, ledger_tools = set(), set()

    for artifact_id, entry in context.active.items():
        kind = entry.get("kind")
        if kind not in ("policy_rule", "hook", "tool"):
            continue
        target = entry.get("payload")
        if kind == "hook":
            ledger_hooks.add(tuple(contract.hook_key(target)))
        elif kind == "tool":
            ledger_tools.add(target.get("name"))
        after = [e for e in mining if e.id > (entry.get("applied_at") or "")]
        uses = sum(1 for e in after for use in e.uses() if use == artifact_id)
        recent_models = [e.model for e in after[-model_window:] if e.model]
        if len(after) >= unused and uses == 0:
            reason = "no recorded use in %d sessions since it was applied" % len(after)
        elif (entry.get("model") and len(recent_models) >= model_window
              and all(m != entry["model"] for m in recent_models)):
            reason = "learned under %s; the last %d sessions ran on %s" % (
                entry["model"], len(recent_models), recent_models[-1])
        else:
            continue
        proposals.append(_proposal(kind, target, reason, artifact_id,
                                   [e.id for e in after],
                                   "retire %s %s: %s" % (kind, artifact_id, reason)))

    for name, info in sorted(context.artifacts["tools"].items()):
        if name in ledger_tools:
            continue
        after = [e for e in mining if e.id > _stamp(info.get("mtime") or 0)]
        calls = sum(1 for e in after for turn in e.turns
                    for event in turn.tool_events if event.get("name") == name)
        if len(after) >= unused and calls == 0:
            reason = "self-defined tool not called in %d sessions" % len(after)
            proposals.append(_proposal("tool", name, reason, None, [e.id for e in after],
                                       "remove tool %r: %s" % (name, reason)))

    recent = list(mining)[-unused:]
    for hook in context.artifacts["hooks"]:
        if hook.get("event") not in TOOL_EVENTS or tuple(contract.hook_key(hook)) in ledger_hooks:
            continue
        if len(recent) >= unused and not _hook_matched(hook, recent):
            reason = "hook matched no tool call in the last %d sessions" % len(recent)
            proposals.append(_proposal("hook", hook, reason, None, [e.id for e in recent],
                                       "remove hook `%s`: %s" % (hook.get("run"), reason)))
    return proposals
