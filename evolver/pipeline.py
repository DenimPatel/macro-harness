"""ingest -> mine -> verify -> gate -> inbox, as plain functions.

The CLI is a thin shell over these, and the tests call them directly.
"""

from . import gate as gate_mod
from . import miners, proposal as proposal_mod, signals, split, traces, verify
from .adapters import macroharness as adapter
from .store import now_stamp


def split_for(store, episodes):
    """Split, and pin whatever was held out, so it stays held out."""
    mining, held = split.split(episodes, pinned=store.pinned_held_out())
    store.pin_held_out(episode.id for episode in held)
    return mining, held


def ingest(store):
    """Read every session log, snapshot the metrics. Returns (episodes, metrics)."""
    episodes = traces.load(store.workspace)
    snapshot = dict(signals.metrics(episodes), at=now_stamp())
    store.append_metrics(snapshot)
    return episodes, snapshot


def context_for(store, thresholds=None):
    return miners.Context(adapter.current_artifacts(store.workspace),
                          active=store.active_artifacts(), thresholds=thresholds)


def mine(store, episodes, thresholds=None, out=print):
    """Run the miners on the mining set; keep what passes static checks."""
    mining, _held = split_for(store, episodes)
    context = context_for(store, thresholds)
    archived = store.archived()
    failed = {row["id"]: row for row in archived if row.get("status") == proposal_mod.FAILED}
    reverted = [row.get("target_id") for row in store.ledger()
                if row.get("action") == "revert"]
    known = {row["id"] for row in store.inbox()}
    kept = []
    for proposal in miners.run_all(mining, context):
        if proposal["id"] in known:
            continue
        before = failed.get(proposal["id"])
        if before is not None and len(proposal["contract"]["predicts_fix"]) <= \
                len(before["contract"]["predicts_fix"]):
            continue  # failed before on the same evidence; wait for more
        problems = verify.check(proposal, context.artifacts, context.active, archived,
                                reverted)
        proposal["verify"] = problems
        if problems:
            proposal["status"] = proposal_mod.FAILED
            store.archive(proposal)
            out("refused %s: %s" % (proposal["id"], "; ".join(problems)))
            continue
        store.put(proposal)
        kept.append(proposal)
        out("mined %s: %s" % (proposal["id"], proposal["effect"]))
    return kept


def gate(store, episodes, live=None, out=print):
    """Gate every mined proposal: pending if it passes, archived as failed if not."""
    mining, held_out = split_for(store, episodes)
    tier1 = gate_mod.make_tier1(store.workspace)
    results = []
    for proposal in store.inbox(status=proposal_mod.MINED):
        result = gate_mod.run(proposal, mining, held_out, tier1, live=live)
        proposal["gate"] = result
        if result["passed"]:
            proposal["status"] = proposal_mod.PENDING
            store.put(proposal)
            out("passed %s" % proposal["id"])
        else:
            proposal["status"] = proposal_mod.FAILED
            store.archive(proposal)
            out("failed %s: %s" % (proposal["id"], "; ".join(result["reasons"])))
        results.append((proposal["id"], result))
    adapter.update_evolved_index(store.workspace,
                                 inbox_pending=len(store.inbox(status=proposal_mod.PENDING)))
    return results


def live_config(store, episodes, api_key_env, budget, runs=1):
    headers = {episode.id: episode.header for episode in episodes}
    return {"cases": store.replayset(), "headers": headers, "api_key_env": api_key_env,
            "budget": budget, "runs": runs}


def run(store, live=None, thresholds=None, out=print):
    episodes, snapshot = ingest(store)
    out("ingested %d session(s), %d turn(s)" % (snapshot["sessions"], snapshot["turns"]))
    mine(store, episodes, thresholds=thresholds, out=out)
    if live is not None:
        live = live_config(store, episodes, live["api_key_env"], live["budget"],
                           live.get("runs", 1))
    gate(store, episodes, live=live, out=out)
    pending = store.inbox(status=proposal_mod.PENDING)
    out("%d proposal(s) waiting for review (mh-evolve review)" % len(pending))
    return pending
