"""A proposal: one change, what it should fix, what it might break, and why.

Every proposal carries a contract written *before* it is tested. `predicts_fix`
names the recorded turns it should change; `risks` says what could go wrong;
`check` says how the gate will decide. That makes each change falsifiable, and
it is the part people skip: proposers are much better at predicting what an
edit fixes than what it breaks, so the risk half is checked against sessions
the proposer never saw.
"""

from .adapters.macroharness import contract
from .store import now_stamp

MINED, PENDING, FAILED, REJECTED, APPLIED = "mined", "pending", "failed", "rejected", "applied"


def make(kind, payload, miner, predicts_fix, risks, check, evidence, turns,
         expires_if=None, model=None, effect=""):
    """Build a proposal dict. Its id is a digest of what it would change."""
    if kind not in contract.ARTIFACT_KINDS:
        raise ValueError("unknown artifact kind %r" % kind)
    return {
        "id": "%s-%s" % (kind.replace("_", "-"), contract.digest([kind, payload])[:10]),
        "kind": kind,
        "payload": payload,
        "effect": effect,
        "contract": {
            "predicts_fix": predicts_fix,
            "risks": list(risks),
            "check": check,
            "expires_if": expires_if or {"unused_sessions": 20},
        },
        "evidence": evidence[:5],
        "provenance": {
            "miner": miner,
            "sessions": sorted({ref["session"] for ref in predicts_fix}),
            "model": model,
            "tainted": any(turn.tainted for turn in turns),
            "created_at": now_stamp(),
        },
        "status": MINED,
        "verify": None,
        "gate": None,
    }


def evidence_from(turns, note=""):
    """A few short excerpts a human can check the claim against."""
    rows = []
    for turn in turns[:5]:
        rows.append({"ref": turn.ref, "user": turn.user_text[:160], "note": note})
    return rows
