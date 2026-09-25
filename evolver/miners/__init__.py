"""Miners: rule-based readers of the mining set that write proposals.

A miner never writes a file and never sees a held-out session. It looks for one
kind of pattern and, when the pattern is strong enough, returns a proposal with
its contract filled in. Whether the proposal is any good is the gate's call.
"""

from ..split import require_mining_set
from . import approvals, failures, restatement, retire, strategy

MINERS = (approvals, failures, strategy, restatement, retire)


class Context:
    """What miners may know besides the mining set: the state in force today."""

    def __init__(self, artifacts, active=None, thresholds=None):
        self.artifacts = artifacts
        self.active = active or {}
        self.thresholds = dict(DEFAULT_THRESHOLDS)
        self.thresholds.update(thresholds or {})


DEFAULT_THRESHOLDS = {
    "approvals_min": 3,          # the same approval, this many times...
    "approvals_sessions": 2,     # ...across at least this many sessions
    "failures_min": 2,           # the same fixable error this many times
    "stops_min": 2,              # turns that ran out of steps
    "stops_share": 0.2,          # ...and at least this share of all turns
    "retry_exhaustion_min": 2,   # model calls that used every retry
    "restatement_min": 3,        # the same instruction this many times
    "retire_unused_sessions": 20,
    "retire_model_sessions": 3,  # recent sessions on a different model
}


def run_all(mining, context, miners=MINERS):
    require_mining_set(mining)
    proposals = []
    seen = set()
    for miner in miners:
        for proposal in miner.mine(mining, context):
            if proposal["id"] not in seen:
                seen.add(proposal["id"])
                proposals.append(proposal)
    return proposals
