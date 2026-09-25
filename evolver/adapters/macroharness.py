"""The one place the evolver touches macro-harness.

Everything else in `evolver/` works on plain dicts: episodes, proposals, replay
outcomes. This module turns those into macro-harness files and back, reuses the
harness's own validators instead of re-implementing them, and builds the
harnesses that replay runs against. Pointing the evolver at another harness
means writing a sibling of this file, not editing the rest of the package.
"""

import json
import os

from macroharness import __main__ as cli
from macroharness import (contract, extensions, hooks as hooks_mod, permissions,
                          session as session_mod, strategy as strategy_mod,
                          tools as tools_mod)

STATE_DIR = cli.STATE_DIR
BUILTIN_TOOLS = ("read_file", "write_file", "edit_file", "run_bash", "task",
                 extensions.DEFINE_TOOL_NAME, "define_hook")
SELF_EXTENSION_TOOLS = (extensions.DEFINE_TOOL_NAME, "define_hook")
TRUST_FILES = ("trusted.json", contract.EVOLVED_INDEX)


class AdapterError(Exception):
    pass


# --- where things are -----------------------------------------------------------

def state_dir(workspace):
    return os.path.join(workspace, STATE_DIR)


def sessions_dir(workspace):
    return os.path.join(state_dir(workspace), "sessions")


def session_files(workspace):
    """Main session logs, oldest first. Subagent logs (`*.sub-N`) are left out:
    they are nested turns of a parent session and replay with it."""
    directory = sessions_dir(workspace)
    if not os.path.isdir(directory):
        return []
    return [os.path.join(directory, entry) for entry in sorted(os.listdir(directory))
            if entry.endswith(".jsonl") and ".sub-" not in entry]


def read_events(path):
    return session_mod.Session(path).events()


# --- validators (the harness's own) ----------------------------------------------

def validate_rule(rule):
    if not isinstance(rule, dict):
        raise AdapterError("a policy rule must be an object")
    if rule.get("verb") not in permissions.VERBS:
        raise AdapterError("rule verb must be one of %s" % ", ".join(permissions.VERBS))
    for key in ("tool", "arg"):
        if not isinstance(rule.get(key), str) or not rule[key]:
            raise AdapterError("rule needs a non-empty string %r" % key)
    return {"tool": rule["tool"], "arg": rule["arg"], "verb": rule["verb"]}


def validate_hook(hook):
    try:
        return hooks_mod.validate({"version": 1, "hooks": [hook]})["hooks"][0]
    except hooks_mod.HookError as error:
        raise AdapterError(str(error))


def validate_tool(definition):
    try:
        return extensions.validate(definition, taken=BUILTIN_TOOLS)
    except extensions.ExtensionError as error:
        raise AdapterError(str(error))


def clamp_strategy(raw):
    try:
        return strategy_mod.clamp(raw)
    except strategy_mod.StrategyError as error:
        raise AdapterError(str(error))


def derive_rule(tool, arguments):
    return permissions.derive_rule(tool, arguments)


def rule_matches(rule, tool, argument):
    return permissions.rule_matches(rule, tool, argument)


def hook_events():
    return hooks_mod.EVENTS


# --- reading what is currently in force ------------------------------------------

def _read_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return default


def current_artifacts(workspace):
    """What the workspace would load today: rules, hooks, tools, strategy."""
    state = state_dir(workspace)
    policy = _read_json(permissions.policy_path(state), permissions.default_policy())
    hooks = _read_json(hooks_mod.hooks_path(state), hooks_mod.default_config())
    tools = {}
    directory = extensions.tools_dir(state)
    if os.path.isdir(directory):
        for entry in sorted(os.listdir(directory)):
            if entry.endswith(".json"):
                path = os.path.join(directory, entry)
                tools[entry[:-5]] = {"definition": _read_json(path, {}),
                                     "mtime": os.path.getmtime(path)}
    strategy_raw = _read_json(strategy_mod.strategy_path(state), {})
    return {"rules": list(policy.get("rules") or []),
            "hooks": list(hooks.get("hooks") or []),
            "tools": tools,
            "strategy": strategy_raw if isinstance(strategy_raw, dict) else {},
            "strategy_defaults": strategy_mod.DEFAULTS,
            "hard_caps": strategy_mod.HARD_CAPS}


# --- writing artifacts -----------------------------------------------------------
# Every write is followed by mark_trusted: the user approved this exact content
# in the evolver's review, which is the human approval trust-on-first-use asks
# for. Each function returns an `undo` record that `revert` hands back.

def _ensure_policy(state):
    path = permissions.ensure_policy(state)
    return path, permissions.load_policy(path)


def add_rule(workspace, rule):
    state = state_dir(workspace)
    path, policy = _ensure_policy(state)
    rule = validate_rule(rule)
    if rule in policy["rules"]:
        raise AdapterError("the policy already has %s" % permissions.describe(rule))
    # First match wins, so a new rule goes in front of the catch-all ask.
    policy["rules"].insert(0, rule)
    permissions.write_policy(path, policy)
    permissions.mark_trusted(state, path)
    return {"op": "remove_rule", "rule": rule}


def remove_rule(workspace, rule):
    state = state_dir(workspace)
    path, policy = _ensure_policy(state)
    if rule not in policy["rules"]:
        raise AdapterError("the policy has no %s" % permissions.describe(rule))
    index = policy["rules"].index(rule)
    policy["rules"].remove(rule)
    permissions.write_policy(path, policy)
    permissions.mark_trusted(state, path)
    return {"op": "add_rule", "rule": rule, "index": index}


def _hooks_file(state):
    path = hooks_mod.ensure_config(state)
    config = hooks_mod.validate(_read_json(path, hooks_mod.default_config()))
    return path, config


def add_hook(workspace, hook):
    state = state_dir(workspace)
    path, config = _hooks_file(state)
    hook = validate_hook(hook)
    if any(contract.hook_key(h) == contract.hook_key(hook) for h in config["hooks"]):
        raise AdapterError("hooks.json already has that hook")
    config["hooks"].append(hook)
    hooks_mod.write_config(path, config)
    permissions.mark_trusted(state, path, key=hooks_mod.TRUST_KEY)
    return {"op": "remove_hook", "hook": hook}


def remove_hook(workspace, hook):
    state = state_dir(workspace)
    path, config = _hooks_file(state)
    kept = [h for h in config["hooks"] if contract.hook_key(h) != contract.hook_key(hook)]
    if len(kept) == len(config["hooks"]):
        raise AdapterError("hooks.json has no such hook")
    removed = [h for h in config["hooks"] if contract.hook_key(h) == contract.hook_key(hook)]
    config["hooks"] = kept
    hooks_mod.write_config(path, config)
    permissions.mark_trusted(state, path, key=hooks_mod.TRUST_KEY)
    return {"op": "add_hook", "hook": removed[0]}


def add_tool(workspace, definition):
    state = state_dir(workspace)
    definition = validate_tool(definition)
    if os.path.exists(extensions.definition_path(state, definition["name"])):
        raise AdapterError("a tool named %r already exists" % definition["name"])
    extensions.save(state, definition)
    return {"op": "remove_tool", "name": definition["name"]}


def remove_tool(workspace, name):
    state = state_dir(workspace)
    path = extensions.definition_path(state, name)
    definition = _read_json(path, None)
    if definition is None:
        raise AdapterError("no tool named %r" % name)
    os.remove(path)
    trusted = permissions.trusted_digests(state)
    if trusted.pop("tool:%s" % name, None) is not None:
        with open(os.path.join(state, "trusted.json"), "w", encoding="utf-8") as handle:
            json.dump(trusted, handle, indent=2)
    return {"op": "restore_tool", "definition": definition}


def set_strategy(workspace, changes):
    """Merge `changes` ({"max_steps": 20, "retry": {"max": 4}}) into strategy.json."""
    state = state_dir(workspace)
    path = strategy_mod.strategy_path(state)
    before = _read_json(path, None)
    current = json.loads(json.dumps(before or {"version": 1}))
    for key, value in changes.items():
        if isinstance(value, dict):
            merged = dict(current.get(key) or {})
            merged.update(value)
            current[key] = merged
        else:
            current[key] = value
    _settings, notes = clamp_strategy(current)
    if notes:
        raise AdapterError("strategy change needs clamping: %s" % "; ".join(notes))
    strategy_mod.write(path, current)
    permissions.mark_trusted(state, path, key=strategy_mod.TRUST_KEY)
    return {"op": "restore_strategy", "content": before}


def undo(workspace, record):
    """Replay an undo record from a ledger entry. Returns the undo of the undo."""
    op = record.get("op")
    if op == "remove_rule":
        return remove_rule(workspace, record["rule"])
    if op == "add_rule":
        state = state_dir(workspace)
        path, policy = _ensure_policy(state)
        rule = validate_rule(record["rule"])
        policy["rules"].insert(min(int(record.get("index") or 0), len(policy["rules"])), rule)
        permissions.write_policy(path, policy)
        permissions.mark_trusted(state, path)
        return {"op": "remove_rule", "rule": rule}
    if op == "remove_hook":
        return remove_hook(workspace, record["hook"])
    if op == "add_hook":
        return add_hook(workspace, record["hook"])
    if op == "remove_tool":
        return remove_tool(workspace, record["name"])
    if op == "restore_tool":
        return add_tool(workspace, record["definition"])
    if op == "restore_strategy":
        state = state_dir(workspace)
        path = strategy_mod.strategy_path(state)
        before = _read_json(path, None)
        if record.get("content") is None:
            if os.path.exists(path):
                os.remove(path)
        else:
            strategy_mod.write(path, record["content"])
            permissions.mark_trusted(state, path, key=strategy_mod.TRUST_KEY)
        return {"op": "restore_strategy", "content": before}
    raise AdapterError("unknown undo operation %r" % op)


def update_evolved_index(workspace, add=None, remove=(), inbox_pending=None):
    """Label applied artifacts so the harness can log their use."""
    state = state_dir(workspace)
    os.makedirs(state, exist_ok=True)
    index = contract.read_evolved_index(state)
    for artifact_id in remove:
        index["artifacts"].pop(artifact_id, None)
    for artifact_id, entry in (add or {}).items():
        index["artifacts"][artifact_id] = entry
    if inbox_pending is not None:
        index["inbox_pending"] = int(inbox_pending)
    contract.write_evolved_index(state, index)


# --- building harnesses for replay -------------------------------------------------

def copy_artifacts(source_workspace, target_workspace):
    """Copy the files that shape behavior -- not sessions, not MCP servers.

    MCP servers are left out on purpose: a replay must not start processes the
    recording talked to. Their calls replay as unknown tools on both sides of a
    comparison, so they cancel out.
    """
    import shutil
    source, target = state_dir(source_workspace), state_dir(target_workspace)
    os.makedirs(target, exist_ok=True)
    for name in ("policy.json", "hooks.json", "strategy.json", "trusted.json"):
        path = os.path.join(source, name)
        if os.path.exists(path):
            shutil.copyfile(path, os.path.join(target, name))
        elif os.path.exists(os.path.join(target, name)):
            os.remove(os.path.join(target, name))
    for directory in ("tools",):
        if os.path.isdir(os.path.join(target, directory)):
            shutil.rmtree(os.path.join(target, directory))
        if os.path.isdir(os.path.join(source, directory)):
            shutil.copytree(os.path.join(source, directory), os.path.join(target, directory))
    mcp_path = os.path.join(target, "mcp.json")
    with open(mcp_path, "w", encoding="utf-8") as handle:
        json.dump({"servers": {}}, handle)


def trust_everything(workspace):
    """In a scratch copy, the files are ours: accept them as they stand."""
    state = state_dir(workspace)
    for path, key in ((permissions.policy_path(state), "policy_sha256"),
                      (hooks_mod.hooks_path(state), hooks_mod.TRUST_KEY),
                      (strategy_mod.strategy_path(state), strategy_mod.TRUST_KEY)):
        if os.path.exists(path):
            permissions.mark_trusted(state, path, key=key)
    directory = extensions.tools_dir(state)
    if os.path.isdir(directory):
        for entry in os.listdir(directory):
            if entry.endswith(".json"):
                permissions.mark_trusted(state, os.path.join(directory, entry),
                                         key="tool:%s" % entry[:-5])


def build_harness(workspace, model, answer_fn=None, model_name="replay",
                  base_url="http://replay.invalid", api_key_env=None, live=False):
    """A harness exactly as `mh` would build it in `workspace`, with `model` injected.

    `answer_fn(label)` answers permission prompts: replay gives the answer the
    user gave in the recording. Output is silenced.
    """
    argv = ["--workspace", workspace, "--model", model_name, "--base-url", base_url,
            "--no-stream"]
    if api_key_env:
        argv += ["--api-key-env", api_key_env]
    args = cli.build_parser().parse_args(argv)
    quiet = lambda *_args, **_kwargs: None  # noqa: E731
    harness = cli.build(args, model=None if live else model,
                        trust_prompt=lambda _question: True, out=quiet)
    if answer_fn is not None:
        harness.permissions.prompt_fn = lambda label, _candidate: answer_fn(label)
    else:
        harness.permissions.interactive = False
    return harness


def tool_events(harness):
    return [event for event in harness.session.events() if event.get("t") == "tool"]


def run_check(workspace, command, timeout=600):
    """Run a replay case's check command in the scratch workspace. Returns the exit code."""
    containment = tools_mod.Containment(workspace, timeout=timeout)
    code, _stdout, _stderr = tools_mod.run_command(containment, command, timeout=timeout)
    return code
