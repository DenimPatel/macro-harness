"""The inbox: proposals that passed the gate, waiting for a human, in one batch.

A proposal is shown as what it will *do*, in words, before what it *is*, in
JSON: approving a shell one-liner you cannot evaluate at a glance is approving
nothing. Next to the effect sit the contract, the evidence it was mined from,
what the gate saw, and -- prominently -- whether it came from a turn where text
the user did not write was in context.
"""

import json

from .adapters import macroharness as adapter
from .apply import ApplyError, apply
from .proposal import PENDING, REJECTED


def render(proposal):
    lines = []
    provenance = proposal["provenance"]
    lines.append("== %s  [%s]" % (proposal["id"], proposal["kind"]))
    lines.append("   effect:  %s" % (proposal.get("effect") or "(none stated)"))
    if provenance.get("tainted"):
        lines.append("   WARNING: mined from turns where outside content (MCP output or a "
                     "network command) was in context. Check the evidence before accepting.")
    contract = proposal["contract"]
    lines.append("   fixes:   %d recorded turn(s) in %s" % (
        len(contract["predicts_fix"]), ", ".join(provenance.get("sessions") or []) or "-"))
    for risk in contract["risks"]:
        lines.append("   risk:    %s" % risk)
    lines.append("   check:   %s" % contract["check"])
    lines.append("   expires: %s" % json.dumps(contract["expires_if"], sort_keys=True))
    for row in proposal.get("evidence") or ():
        ref = row["ref"]
        lines.append("   seen:    %s turn %s: %s%s" % (
            ref["session"], ref["turn"], (row.get("user") or "")[:80],
            (" (%s)" % row["note"]) if row.get("note") else ""))
    gate = proposal.get("gate") or {}
    tier1 = gate.get("tier1") or {}
    if tier1.get("predicted"):
        predicted = tier1["predicted"]
        if predicted.get("effect"):
            lines.append("   tier 1:  %d predicted %s on the turns it claims to fix"
                         % (predicted["hits"], predicted["effect"].replace("_", " ")))
        else:
            lines.append("   tier 1:  predicted effect %s" % predicted.get("note"))
    if tier1.get("held_out"):
        held = tier1["held_out"]
        lines.append("   held-out (%d sessions): %d new errors, %d new failed commands, "
                     "%d new blocks, %d new denials, %d prompts removed, %d results changed" % (
                         len(held["sessions"]), held["new_errors"],
                         held.get("new_failures", 0), held["new_blocks"],
                         held["new_denials"], held["asks_removed"], held["changed_results"]))
    if gate.get("tier2"):
        t2 = gate["tier2"]
        lines.append("   tier 2:  baseline %d/%d passed, %d tokens; candidate %d/%d, %d tokens"
                     % (t2["baseline"]["passed"], t2["baseline"]["checked"],
                        t2["baseline"]["tokens"], t2["candidate"]["passed"],
                        t2["candidate"]["checked"], t2["candidate"]["tokens"]))
    if gate.get("note"):
        lines.append("   note:    %s" % gate["note"])
    lines.append("   change:  %s" % json.dumps(proposal["payload"], sort_keys=True))
    return "\n".join(lines)


def _prompt(question):
    try:
        answer = input(question)
    except EOFError:
        return "s"
    return answer.strip().lower()[:1] or "s"


def review(store, answer_fn=None, accept_all_untainted=False, out=print, model=None):
    """Walk every pending proposal: y applies, n rejects (archived), s skips.

    `accept_all_untainted` applies every pending proposal that was not mined
    from a tainted turn, without asking. It exists for tests and scripted setups;
    tainted proposals are always left for a human.
    """
    pending = store.inbox(status=PENDING)
    if not pending:
        out("inbox is empty")
        return {"applied": [], "rejected": [], "skipped": []}
    answer_fn = answer_fn or _prompt
    summary = {"applied": [], "rejected": [], "skipped": []}
    for proposal in pending:
        out(render(proposal))
        if accept_all_untainted:
            answer = "s" if proposal["provenance"].get("tainted") else "y"
        else:
            answer = answer_fn("   apply? [y]es / [n]o / [s]kip > ")
        if answer == "y":
            try:
                apply(store, proposal, model=model)
            except ApplyError as error:
                out("   could not apply: %s" % error)
                summary["skipped"].append(proposal["id"])
                continue
            out("   applied.")
            summary["applied"].append(proposal["id"])
        elif answer == "n":
            proposal["status"] = REJECTED
            store.archive(proposal)
            out("   rejected; it will not be proposed again.")
            summary["rejected"].append(proposal["id"])
        else:
            summary["skipped"].append(proposal["id"])
    adapter.update_evolved_index(store.workspace,
                                 inbox_pending=len(store.inbox(status=PENDING)))
    return summary
