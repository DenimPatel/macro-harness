"""A scripted model and a harness factory, so every test runs offline.

The fake model satisfies the same interface as macroharness.model.Model, which
is the whole point of keeping the client behind one method: no API key, no spend,
no network, and the transcripts are exact.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from macroharness import context, mcp, permissions, session as session_mod, tools as tools_mod
from macroharness.loop import Harness
from macroharness.model import Completion
from macroharness.subagents import task_tool


def assistant(content=None, tool_calls=None, usage=None):
    message = {"role": "assistant"}
    if content is not None:
        message["content"] = content
    else:
        message["content"] = ""
    if tool_calls:
        message["tool_calls"] = tool_calls
    if usage:
        message["usage"] = usage
    return message


def call(name, arguments, call_id="call_1"):
    if isinstance(arguments, (dict, list)):
        arguments = json.dumps(arguments)
    return {"id": call_id, "type": "function",
            "function": {"name": name, "arguments": arguments}}


class ScriptedModel:
    """Pops one canned reply per call. An Exception reply is raised instead."""

    def __init__(self, replies, usage=None):
        self.replies = list(replies)
        self.requests = []
        self.usage = usage or {}

    def complete(self, messages, tools, on_text=None):
        self.requests.append({"messages": [dict(m) for m in messages],
                              "tools": list(tools or [])})
        if not self.replies:
            raise AssertionError("the scripted model ran out of replies")
        raw = self.replies.pop(0)
        if isinstance(raw, Exception):
            raise raw
        message = dict(raw)
        usage = message.pop("usage", None) or self.usage
        if on_text is not None and message.get("content"):
            on_text(message["content"])
        return Completion(message, usage)


def load_json(path):
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


class Collector:
    """Captures output so tests can assert on what the user would have seen."""

    def __init__(self):
        self.lines = []

    def __call__(self, text=""):
        self.lines.append(text)

    @property
    def text(self):
        return "\n".join(self.lines)


def build_harness(root, replies, rules=None, interactive=True, answers=None,
                  budget=None, max_steps=8, out=None, mcp_config=None,
                  timeout=10, extra_tools=None):
    """A fully wired Harness with the fake model, in a throwaway workspace."""
    state = os.path.join(root, ".macroharness")
    sessions_dir = os.path.join(state, "sessions")
    os.makedirs(sessions_dir, exist_ok=True)
    policy = {"version": 1, "rules": list(rules) if rules is not None
              else [{"tool": "*", "arg": "*", "verb": "allow"}]}
    policy_path = permissions.policy_path(state)
    permissions.write_policy(policy_path, policy)
    queue = list(answers or [])

    def prompt_fn(_label, _candidate):
        return queue.pop(0) if queue else "d"

    console = permissions.Permissions(policy, policy_path, state_dir=state,
                                      interactive=interactive, prompt_fn=prompt_fn)
    output = out if out is not None else Collector()
    session = session_mod.Session.create(sessions_dir, "test")
    containment = tools_mod.Containment(root, timeout=timeout)
    registry = tools_mod.default_registry(containment)
    for tool in extra_tools or ():
        registry.register(tool)
    model = ScriptedModel(replies)
    harness = Harness(
        model=model, registry=registry, permissions=console, session=session,
        accounting=context.Accounting(model_name="scripted"), root=root,
        max_steps=max_steps, budget=budget, interactive=interactive,
        sessions_dir=sessions_dir, out=output, on_text=None,
    )
    registry.register(task_tool(harness.spawn_subagent))
    if mcp_config is not None:
        harness.mcp_servers = mcp.load_servers(registry, mcp_config, root, out=output)
    return harness, model, output
