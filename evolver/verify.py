"""Static checks: refuse a proposal before spending a replay on it.

Shape is checked by the harness's own validators, reached through the adapter,
so the evolver can never accept an artifact the harness would refuse to load.
On top of shape, a handful of rules that no miner is trusted to follow on its
own:

- no standing grants: no allow rule for the tools that extend the harness, no
  allow rule for any tool with a bare `*` argument, no allow-everything rule;
- no hook text substitution: a hook's command may read the argument only as
  the quoted `"$MH_ARG"` variable, never through `{arg}` or `{tool}`;
- no network in a hook command, and hooks come from the fixed catalogue;
- a strategy change must fit inside the hard caps without clamping;
- nothing already rejected once is proposed again, and nothing already in force
  is applied twice.

Returns a list of problems. An empty list means the proposal may be gated.
"""

import re

from .adapters import macroharness as adapter
from .miners.failures import catalogue_runs

NETWORK = re.compile(r"\b(curl|wget|nc|ssh|scp|rsync|ftp|telnet)\b|https?://")
SHELL_META = (";", "&", "|", "`", "$(", ">")


def _rule_problems(rule):
    problems = []
    try:
        rule = adapter.validate_rule(rule)
    except adapter.AdapterError as error:
        return [str(error)]
    if rule["verb"] != "allow":
        return problems
    if rule["tool"] in adapter.SELF_EXTENSION_TOOLS or rule["tool"].startswith("define_"):
        problems.append("an allow rule for %s would approve every future self-extension"
                        % rule["tool"])
    if rule["tool"] in ("*", "") or "*" in rule["tool"]:
        problems.append("an allow rule must name one tool, not %r" % rule["tool"])
    if rule["arg"].strip() in ("*", ""):
        problems.append("an allow rule with argument %r grants the whole tool" % rule["arg"])
    if rule["tool"] == "run_bash" and "*" in rule["arg"] and any(
            token in rule["arg"] for token in SHELL_META):
        problems.append("a wildcard rule may not span shell metacharacters")
    return problems


def _hook_problems(hook):
    problems = []
    try:
        clean = adapter.validate_hook(hook)
    except adapter.AdapterError as error:
        return [str(error)]
    run = clean["run"]
    if "{arg}" in run or "{tool}" in run:
        problems.append("hooks must read the argument as \"$MH_ARG\", not by substitution")
    if NETWORK.search(run):
        problems.append("hook commands may not use the network")
    if run not in catalogue_runs():
        problems.append("hook command is not in the reviewed catalogue")
    if clean["blocking"]:
        problems.append("evolved hooks may inform the model, not block it")
    return problems


def _strategy_problems(changes, artifacts):
    if not isinstance(changes, dict) or not changes:
        return ["a strategy change must set at least one value"]
    merged = dict(artifacts.get("strategy") or {})
    for key, value in changes.items():
        if key in ("base_url", "api_key_env", "model"):
            return ["%s is a trust boundary, not a strategy setting" % key]
        if isinstance(value, dict):
            inner = dict(merged.get(key) or {})
            inner.update(value)
            merged[key] = inner
        else:
            merged[key] = value
    try:
        _settings, notes = adapter.clamp_strategy(merged)
    except adapter.AdapterError as error:
        return [str(error)]
    return ["needs clamping: %s" % note for note in notes]


def _retire_problems(payload, artifacts, active):
    kind, target = payload.get("target_kind"), payload.get("target")
    if payload.get("ledger_id"):
        if payload["ledger_id"] not in active:
            return ["ledger artifact %s is not in force" % payload["ledger_id"]]
        return []
    if kind == "tool":
        return [] if target in artifacts["tools"] else ["no tool named %r" % target]
    if kind == "hook":
        present = [adapter.contract.hook_key(h) for h in artifacts["hooks"]]
        return [] if adapter.contract.hook_key(target or {}) in present else ["no such hook"]
    if kind == "policy_rule":
        return [] if target in artifacts["rules"] else ["no such rule"]
    return ["cannot retire a %r" % kind]


def check(proposal, artifacts, active=None, archived=(), reverted=()):
    """`archived` rows a human rejected, and `reverted` ids, are never proposed again.

    A proposal that merely failed the gate may come back: the gate may have had
    no held-out sessions yet, and evidence grows.
    """
    active = active or {}
    kind, payload = proposal.get("kind"), proposal.get("payload")
    problems = []
    rejected = {row["id"] for row in archived if row.get("status") == "rejected"}
    if proposal.get("id") in rejected:
        problems.append("this exact change was rejected before")
    if proposal.get("id") in set(reverted):
        problems.append("this exact change was applied and then reverted")
    if proposal.get("id") in active:
        problems.append("this exact change is already applied")

    if kind == "policy_rule":
        problems += _rule_problems(payload)
        if payload in artifacts["rules"]:
            problems.append("the policy already has this rule")
    elif kind == "hook":
        problems += _hook_problems(payload)
    elif kind == "tool":
        try:
            definition = adapter.validate_tool(payload)
        except adapter.AdapterError as error:
            problems.append(str(error))
        else:
            if definition["name"] in artifacts["tools"]:
                problems.append("a tool named %r already exists" % definition["name"])
            if NETWORK.search(definition["command"]):
                problems.append("tool commands may not use the network")
    elif kind == "strategy":
        problems += _strategy_problems(payload, artifacts)
    elif kind == "fact":
        text = (payload or {}).get("text")
        if not isinstance(text, str) or not text.strip() or len(text) > 500:
            problems.append("a fact is one non-empty line of at most 500 characters")
    elif kind == "retire":
        problems += _retire_problems(payload or {}, artifacts, active)
    else:
        problems.append("unknown kind %r" % kind)
    return problems
