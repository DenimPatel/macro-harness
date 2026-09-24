"""Applying a reviewed proposal, and taking it back out.

`write_artifact` is the single place a proposal becomes a harness file; replay
uses it on a scratch copy and `apply` uses it on the real workspace, so what was
tested is byte-for-byte what lands. Every apply appends a ledger entry carrying
the `undo` record the adapter returned, which is all `revert` needs.

The review is the human approval: files written here are marked trusted, the
same as an `always allow` the user typed at the prompt.
"""

from .adapters import macroharness as adapter
from .adapters.macroharness import contract
from .proposal import APPLIED
from .store import now_stamp


class ApplyError(Exception):
    pass


def write_artifact(workspace, proposal, store=None):
    """Make the change. Returns (undo record, evolved-index entry or None)."""
    kind, payload = proposal["kind"], proposal["payload"]
    try:
        if kind == contract.POLICY_RULE:
            return adapter.add_rule(workspace, payload), {"kind": kind, "match": payload}
        if kind == contract.HOOK:
            undo = adapter.add_hook(workspace, payload)
            return undo, {"kind": kind, "match": undo["hook"]}
        if kind == contract.TOOL_DEF:
            undo = adapter.add_tool(workspace, payload)
            return undo, {"kind": kind, "match": {"name": undo["name"]}}
        if kind == contract.STRATEGY:
            return adapter.set_strategy(workspace, payload), None
        if kind == contract.FACT:
            if store is not None:
                facts = store.facts()
                facts.append({"id": proposal["id"], "text": payload["text"],
                              "scope": payload.get("scope", "project")})
                store.save_facts(facts)
            return {"op": "remove_fact", "id": proposal["id"]}, None
        if kind == contract.RETIRE:
            return _retire(workspace, payload), None
    except adapter.AdapterError as error:
        raise ApplyError(str(error))
    raise ApplyError("cannot apply a %r" % kind)


def _retire(workspace, payload):
    kind, target = payload["target_kind"], payload["target"]
    if kind == contract.POLICY_RULE:
        return adapter.remove_rule(workspace, target)
    if kind == contract.HOOK:
        return adapter.remove_hook(workspace, target)
    if kind == contract.TOOL_DEF:
        name = target if isinstance(target, str) else target.get("name")
        return adapter.remove_tool(workspace, name)
    raise ApplyError("cannot retire a %r" % kind)


def _refresh_inbox_count(store):
    adapter.update_evolved_index(store.workspace,
                                 inbox_pending=len(store.inbox(status="pending")))


def apply(store, proposal, model=None):
    workspace = store.workspace
    undo, index_entry = write_artifact(workspace, proposal, store=store)
    entry = {
        "id": proposal["id"], "action": "apply", "kind": proposal["kind"],
        "payload": proposal["payload"], "target": index_entry,
        "digest": contract.digest(proposal["payload"]), "model": model,
        "applied_at": now_stamp(), "expires_if": proposal["contract"]["expires_if"],
        "provenance": proposal["provenance"], "undo": undo,
    }
    store.append_ledger(entry)
    add = {proposal["id"]: index_entry} if index_entry else None
    remove = ()
    ledger_id = (proposal["payload"] or {}).get("ledger_id") \
        if proposal["kind"] == contract.RETIRE else None
    if ledger_id:
        store.append_ledger({"id": "retire-of-%s" % ledger_id, "action": "retire",
                             "target_id": ledger_id, "by": proposal["id"],
                             "applied_at": now_stamp()})
        remove = (ledger_id,)
    adapter.update_evolved_index(workspace, add=add, remove=remove)
    proposal["status"] = APPLIED
    store.archive(proposal)
    _refresh_inbox_count(store)
    return entry


def revert(store, artifact_id):
    """Undo one applied proposal by its id."""
    active = store.active_artifacts()
    entry = active.get(artifact_id)
    if entry is None:
        raise ApplyError("%s is not an applied, active artifact" % artifact_id)
    undo = entry.get("undo") or {}
    try:
        if undo.get("op") == "remove_fact":
            store.save_facts([f for f in store.facts() if f.get("id") != artifact_id])
            redo = None
        else:
            redo = adapter.undo(store.workspace, undo)
    except adapter.AdapterError as error:
        raise ApplyError(str(error))
    store.append_ledger({"id": "revert-of-%s" % artifact_id, "action": "revert",
                         "target_id": artifact_id, "redo": redo, "applied_at": now_stamp()})
    adapter.update_evolved_index(store.workspace, remove=(artifact_id,))

    # Reverting a retirement brings the retired ledger artifact back into force.
    ledger_id = (entry.get("payload") or {}).get("ledger_id") \
        if entry.get("kind") == contract.RETIRE else None
    if ledger_id:
        original = next((row for row in reversed(store.ledger())
                         if row.get("id") == ledger_id and row.get("action") == "apply"), None)
        if original is not None:
            reinstated = dict(original, applied_at=now_stamp(), reinstated_by=artifact_id)
            store.append_ledger(reinstated)
            if original.get("target"):
                adapter.update_evolved_index(store.workspace,
                                             add={ledger_id: original["target"]})
    return entry
