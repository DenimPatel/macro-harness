"""The same approval, over and over: propose the rule the user keeps granting.

This is `always allow` run offline and with evidence. The rule is derived with
the harness's own `derive_rule`, so it is exactly as narrow as the one the user
would have been offered at the prompt. Two kinds of rule are never proposed: an
allow for the tools that let the harness extend itself (one approval would
authorize every future definition), and an allow whose argument is a bare `*`
(one approval would authorize everything the tool can do).
"""

from ..adapters import macroharness as adapter
from ..proposal import evidence_from, make
from ..signals import asks
from ..split import require_mining_set


def mine(mining, context):
    require_mining_set(mining)
    threshold = context.thresholds["approvals_min"]
    min_sessions = context.thresholds["approvals_sessions"]
    existing = [rule for rule in context.artifacts["rules"] if rule.get("verb") == "allow"]
    groups = {}
    for ask in asks(mining):
        if ask["answer"] not in ("y", "a"):
            continue
        rule = adapter.derive_rule(ask["tool"], ask["arguments"])
        if ask["tool"] in adapter.SELF_EXTENSION_TOOLS or rule["arg"] in ("*", ""):
            continue
        key = (rule["tool"], rule["arg"])
        groups.setdefault(key, {"rule": rule, "asks": []})["asks"].append(ask)

    proposals = []
    for group in groups.values():
        rule, seen = group["rule"], group["asks"]
        sessions = {ask["ref"]["session"] for ask in seen}
        if len(seen) < threshold or len(sessions) < min_sessions:
            continue
        if rule in existing:
            continue
        turns = [ask["turn"] for ask in seen]
        refs = []
        for ask in seen:
            if ask["ref"] not in refs:
                refs.append(ask["ref"])
        proposals.append(make(
            "policy_rule", rule, miner="approvals",
            predicts_fix=refs,
            risks=["calls matching %s %r will run without asking" % (rule["tool"], rule["arg"]),
                   "a matching call you would have denied will not be caught"],
            check="tier 1: prompts disappear on the predicted turns; no new errors, "
                  "blocks or denials on held-out sessions",
            evidence=evidence_from(turns, "approved %s %s" % (ask["tool"], ask["arg"][:60])),
            turns=turns,
            effect="allow %s calls matching %r without asking (approved %d times in %d sessions)"
                   % (rule["tool"], rule["arg"], len(seen), len(sessions)),
        ))
    return proposals
