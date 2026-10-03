"""Subagents: one `task` tool that runs a nested loop with a fresh context.

The point is context isolation, not concurrency. A subagent gets its own message
list, the same tools and the same policy, and hands back only its final answer,
so a large search does not have to live in the parent's transcript forever.

Two additions make it usable for orchestration:

- Agent profiles. `.macroharness/agents/<name>.md` is a role: a short header
  (description, a tool allowlist, a step limit) and a system prompt body.
  `task(prompt, agent="verifier")` runs that role. A profile can only narrow
  the tools; it never widens the policy.
- Concurrency. Several `task` calls in one round run in parallel, each with its
  own log, and their answers still land in call order.
"""

import os

from .tools import Tool

AGENTS_DIR = "agents"
HEADER_KEYS = ("description", "tools", "max_steps")


class AgentError(Exception):
    pass


class Agent:
    def __init__(self, name, description="", tools=None, max_steps=None, prompt=""):
        self.name = name
        self.description = description
        self.tools = tools  # None means every tool the parent has, minus `task`
        self.max_steps = max_steps
        self.prompt = prompt


def parse_agent(name, text):
    """A `---` header of `key: value` lines, then the system prompt.

    Deliberately not YAML: three keys, one per line, so the format stays
    readable without a dependency.
    """
    fields = {}
    body = text
    if text.startswith("---"):
        lines = text.splitlines()
        try:
            end = lines.index("---", 1)
        except ValueError:
            raise AgentError("agent %r: header has no closing ---" % name)
        for number, line in enumerate(lines[1:end], start=2):
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            key, separator, value = line.partition(":")
            key = key.strip()
            if not separator or key not in HEADER_KEYS:
                raise AgentError("agent %r line %d: expected one of %s"
                                 % (name, number, ", ".join(HEADER_KEYS)))
            fields[key] = value.strip()
        body = "\n".join(lines[end + 1:])
    tools = None
    if fields.get("tools"):
        tools = [item.strip() for item in fields["tools"].split(",") if item.strip()]
    max_steps = None
    if fields.get("max_steps"):
        try:
            max_steps = int(fields["max_steps"])
        except ValueError:
            raise AgentError("agent %r: max_steps must be an integer" % name)
        if max_steps < 1:
            raise AgentError("agent %r: max_steps must be at least 1" % name)
    return Agent(name, fields.get("description", ""), tools, max_steps, body.strip())


def load_agents(state_dir):
    """Every `<name>.md` in `<state>/agents/`, by name. Missing dir is fine."""
    directory = os.path.join(state_dir, AGENTS_DIR)
    agents = {}
    if not os.path.isdir(directory):
        return agents
    for entry in sorted(os.listdir(directory)):
        if not entry.endswith(".md"):
            continue
        name = entry[:-3]
        with open(os.path.join(directory, entry), "r", encoding="utf-8") as handle:
            agents[name] = parse_agent(name, handle.read())
    return agents


TASK_DESCRIPTION = (
    "Delegate a self-contained subtask to a fresh agent. The subagent has its own "
    "message list, the same tools and the same permissions, and returns only its "
    "final answer. Use it to keep large searches or file reads out of this "
    "conversation. Several task calls in one reply run in parallel. Subagents "
    "cannot ask the user anything, and cannot delegate."
)


def task_schema(agents):
    properties = {
        "prompt": {
            "type": "string",
            "description": "A self-contained task. The subagent cannot see this conversation.",
        },
    }
    if agents:
        properties["agent"] = {
            "type": "string",
            "enum": sorted(agents),
            "description": "Optional named role; omit for a general-purpose subagent.",
        }
    return {"type": "object", "properties": properties, "required": ["prompt"]}


def task_description(agents):
    if not agents:
        return TASK_DESCRIPTION
    roles = "; ".join("%s: %s" % (name, agents[name].description or "(no description)")
                      for name in sorted(agents))
    return "%s Named agents: %s." % (TASK_DESCRIPTION, roles)


def task_tool(spawn, agents=None):
    """Build the `task` tool around a spawn(prompt, agent=None) -> text callable."""
    agents = agents or {}

    def task(prompt, agent=None):
        if agent:
            return spawn(prompt, agent=agent)
        return spawn(prompt)

    return Tool("task", task_description(agents), task_schema(agents), task,
                read_only=False, concurrent=True)
