"""The gate: the part that can say no.

Generating candidate changes is easy; deciding which ones help is the hard part,
and it is the part this module exists for. A proposal passes only if:

1. static checks found nothing (`verify`);
2. tier 1, on the sessions it claims to fix, shows the change it predicted --
   prompts removed, a hook firing, a step limit no longer hit;
3. tier 1, on held-out sessions the proposer never saw, shows no new errors,
   no commands that newly exit non-zero, no new blocks and no new denials;
4. tier 2, when it is run, shows the held-out pass rate did not fall and the
   token cost did not rise by more than 10%.

A proposal the gate cannot test (there are no held-out sessions yet) fails. An
untested change is not a passed change.
"""

from .adapters.macroharness import contract
from .replay import Tier1, tier2

MAX_PREDICTED_SESSIONS = 5
MAX_HELD_OUT_SESSIONS = 10
TOKEN_TOLERANCE = 1.10

# What each kind must visibly do on the turns it says it fixes.
PREDICTED_EFFECT = {
    contract.POLICY_RULE: "asks_removed",
    contract.HOOK: "changed_results",
    "strategy:max_steps": "stops_removed",
}
REGRESSIONS = ("new_errors", "new_failures", "new_blocks", "new_denials")


def _predicted_key(proposal):
    if proposal["kind"] == contract.STRATEGY:
        return PREDICTED_EFFECT.get(proposal["provenance"]["miner"])
    return PREDICTED_EFFECT.get(proposal["kind"])


def _refs_in(refs, predicted):
    wanted = {(ref["session"], ref["turn"]) for ref in predicted}
    return [ref for ref in refs if (ref["session"], ref["turn"]) in wanted]


def run(proposal, mining, held_out, tier1, live=None):
    """Gate one verified proposal. Returns the result stored on the proposal."""
    result = {"passed": False, "reasons": [], "tier1": {}, "tier2": None,
              "note": "tier 1 replays recorded model output; it checks mechanics, "
                      "not how a model would respond to the change"}
    if proposal["kind"] == contract.FACT:
        result["passed"] = True
        result["note"] = None
        result["reasons"].append("facts are recorded, not injected, so there is nothing "
                                 "to replay yet")
        return result
    if not held_out:
        result["reasons"].append("no held-out sessions yet; cannot check for regressions")
        return result

    predicted = proposal["contract"]["predicts_fix"]
    key = _predicted_key(proposal)
    if key is not None:
        sessions = [e for e in mining
                    if e.id in {ref["session"] for ref in predicted}][-MAX_PREDICTED_SESSIONS:]
        diff = tier1.diff(sessions, proposal)
        hits = _refs_in(diff.get(key, []), predicted)
        result["tier1"]["predicted"] = {"effect": key, "hits": len(hits),
                                        "sessions": [e.id for e in sessions]}
        if not hits:
            result["reasons"].append("tier 1 did not show the predicted %s on the turns it "
                                     "claims to fix" % key.replace("_", " "))
    else:
        result["tier1"]["predicted"] = {"effect": None,
                                        "note": "not observable in tier 1"}

    held = list(held_out)[-MAX_HELD_OUT_SESSIONS:]
    diff = tier1.diff(held, proposal)
    counts = {name: len(diff.get(name, [])) for name in
              REGRESSIONS + ("asks_removed", "new_asks", "fixed_errors", "changed_results",
                             "stops_removed", "new_stops")}
    result["tier1"]["held_out"] = dict(counts, sessions=[e.id for e in held])
    for name in REGRESSIONS:
        if counts[name]:
            result["reasons"].append("held-out regression: %d %s" % (
                counts[name], name.replace("_", " ")))

    if live is not None and live.get("cases"):
        outcome = tier2(tier1.workspace, live["cases"], live["headers"], proposal,
                        live["api_key_env"], live["budget"], live.get("runs", 1))
        result["tier2"] = _score(outcome)
        result["reasons"] += result["tier2"]["reasons"]

    result["passed"] = not result["reasons"]
    return result


def _score(outcome):
    def summary(rows):
        checked = [row for row in rows if row["passed"] is not None]
        passed = sum(1 for row in checked if row["passed"])
        tokens = sum(row["tokens"] for row in rows)
        return {"runs": len(rows), "passed": passed, "checked": len(checked),
                "tokens": tokens}

    base, cand = summary(outcome["baseline"]), summary(outcome["candidate"])
    reasons = []
    if outcome["budget_exhausted"]:
        reasons.append("tier 2 ran out of budget before finishing")
    if base["checked"] and cand["checked"]:
        if cand["passed"] / cand["checked"] < base["passed"] / base["checked"]:
            reasons.append("tier 2 pass rate fell: %d/%d -> %d/%d" % (
                base["passed"], base["checked"], cand["passed"], cand["checked"]))
    if base["tokens"] and cand["runs"] == base["runs"] and \
            cand["tokens"] > base["tokens"] * TOKEN_TOLERANCE:
        reasons.append("tier 2 tokens rose more than 10%%: %d -> %d" % (
            base["tokens"], cand["tokens"]))
    return {"baseline": base, "candidate": cand, "reasons": reasons}


def make_tier1(workspace):
    return Tier1(workspace)
