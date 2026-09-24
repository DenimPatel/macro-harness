"""The same instruction, given again: propose remembering it.

A user who types "use tabs, not spaces" in five sessions is telling the harness
something it failed to keep. The proposal is a `fact`. Until macro-harness has a
memory layer the evolver only records accepted facts in its own store, so this
miner's output is a to-do list for that layer, and says so.
"""

from ..proposal import evidence_from, make
from ..signals import restatements
from ..split import require_mining_set
from ..traces import by_id


def mine(mining, context):
    require_mining_set(mining)
    episodes = by_id(mining)
    proposals = []
    for cluster in restatements(mining):
        if len(cluster["refs"]) < context.thresholds["restatement_min"]:
            continue
        turns = [episodes[ref["session"]].turns[ref["turn"]] for ref in cluster["refs"]]
        text = cluster["text"][:500]
        proposals.append(make(
            "fact", {"text": text, "scope": "project"}, miner="restatement",
            predicts_fix=cluster["refs"],
            risks=["a fact true in one situation is applied in another",
                   "every fact costs context on every request once memory exists"],
            check="not replayable: facts are stored, not yet injected into the harness",
            evidence=evidence_from(turns, "said again"),
            turns=turns,
            effect="remember: %r (said %d times in %d sessions)"
                   % (text[:120], len(cluster["refs"]),
                      len({ref["session"] for ref in cluster["refs"]})),
        ))
    return proposals
