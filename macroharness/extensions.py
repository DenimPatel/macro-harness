"""Extension tools: capabilities added to a running harness, as data.

The built-in tools are four `registry.register(Tool(...))` calls in
`tools.default_registry`. An extension tool is the same call, with the schema
and the body read from a JSON file instead of written in Python. That is the
whole design, and it is deliberate: there is no such thing here as a
"built-in" tool and a "plugin" tool, because two registration paths means the
second one is second class and rots.

    .macroharness/tools/run_tests.json
    {
      "name": "run_tests",
      "description": "Run the project test suite.",
      "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
      "command": "python3 -m unittest discover -s {path}",
      "read_only": true
    }

A definition is a *command template*, not Python, for two reasons. Arbitrary
Python would run inside the harness process, outside `Containment`, and could
edit the registry that is supposed to constrain it. A command runs through
`tools.run_command`, so an extension tool inherits the same jail, the same
timeout and the same scrubbed environment as `run_bash` -- and it still crosses
the permission pipeline on every call, because `loop` does not know or care
where a tool came from.

Loaded at startup, and `define_tool` adds one mid-session. Both end at
`registry.register`, so a tool defined four turns ago is indistinguishable from
`read_file` by the time the loop dispatches it.
"""

import json
import os
import re
import shlex

from . import permissions
from .tools import TIMED_OUT, Tool, run_command

NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
PLACEHOLDER = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")
EXTENSION_TIMEOUT = 120
DEFINE_TOOL_NAME = "define_tool"


class ExtensionError(Exception):
    """A definition we refuse to load. Never raised past the registry call."""


def tools_dir(state_dir):
    return os.path.join(state_dir, "tools")


def validate(definition, taken=()):
    """Refuse a definition rather than register a tool we cannot explain.

    `taken` is the set of names already registered. Shadowing is refused
    outright: if an extension could redefine `read_file`, every containment
    guarantee stated in terms of `read_file` would be a guess.
    """
    if not isinstance(definition, dict):
        raise ExtensionError("a tool definition must be a JSON object")
    name = definition.get("name")
    if not isinstance(name, str) or not NAME_PATTERN.match(name):
        raise ExtensionError(
            "tool name %r must match [a-z][a-z0-9_]{0,31}" % (name,))
    if name in taken:
        raise ExtensionError(
            "%r is already registered; extensions may not shadow an existing tool" % name)
    description = definition.get("description")
    if not isinstance(description, str) or not description.strip():
        raise ExtensionError("%s needs a non-empty description" % name)
    command = definition.get("command")
    if not isinstance(command, str) or not command.strip():
        raise ExtensionError("%s needs a non-empty command" % name)

    parameters = definition.get("parameters") or {"type": "object", "properties": {}}
    if not isinstance(parameters, dict) or parameters.get("type") != "object":
        raise ExtensionError("%s parameters must be a JSON Schema object" % name)
    properties = parameters.get("properties")
    if properties is None:
        properties = {}
        parameters = dict(parameters, properties=properties)
    if not isinstance(properties, dict):
        raise ExtensionError("%s parameters.properties must be an object" % name)

    # Every {placeholder} must be a declared parameter. A template referring to
    # something the model cannot pass is a tool that is always broken, and one
    # referring to something undeclared is a tool whose inputs are not reviewable.
    unknown = sorted(set(PLACEHOLDER.findall(command)) - set(properties))
    if unknown:
        raise ExtensionError(
            "%s command uses undeclared placeholder(s): %s" % (name, ", ".join(unknown)))

    return {
        "name": name,
        "description": description.strip(),
        "parameters": parameters,
        "command": command,
        "read_only": bool(definition.get("read_only", False)),
    }


def render(command, arguments):
    """Substitute arguments into the template, one shell-quoted value at a time.

    Not `str.format`: that resolves attributes and indexes, so `{x.__class__}`
    on a model-supplied mapping is a sentence in a language we did not intend to
    accept. A regex plus `shlex.quote` keeps every substituted value a single
    shell word, so an argument cannot become an operator.
    """
    def substitute(match):
        value = arguments.get(match.group(1))
        if value is None:
            return "''"
        if isinstance(value, bool):
            return "true" if value else "false"
        return shlex.quote(str(value))
    return PLACEHOLDER.sub(substitute, command)


def build_tool(definition, containment, timeout=EXTENSION_TIMEOUT):
    """Turn a validated definition into the same Tool the built-ins produce."""
    spec = definition

    def call(**arguments):
        command = render(spec["command"], arguments)
        code, stdout, stderr = run_command(containment, command, timeout=timeout)
        if code == TIMED_OUT:
            return "error: %s timed out after %ds" % (spec["name"], timeout)
        return "exit %d\nstdout:\n%s\nstderr:\n%s" % (code, stdout, stderr)

    return Tool(spec["name"], spec["description"], spec["parameters"], call,
                read_only=spec["read_only"])


def definition_path(state_dir, name):
    return os.path.join(tools_dir(state_dir), name + ".json")


def save(state_dir, definition):
    """Write a definition and trust it: it was just approved to get here."""
    directory = tools_dir(state_dir)
    os.makedirs(directory, exist_ok=True)
    path = definition_path(state_dir, definition["name"])
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(definition, handle, indent=2)
        handle.write("\n")
    os.replace(temporary, path)
    permissions.mark_trusted(state_dir, path, key="tool:%s" % definition["name"])
    return path


def load_all(registry, containment, state_dir, interactive=True, out=print):
    """Register every definition on disk. Trust on first use, same as the policy.

    A definition is a command this harness will run, and it lives in the
    workspace, so a cloned repository could ship one. Printing it once before it
    is registered is the mitigation -- the same bargain `policy.json` makes, for
    the same reason. An untrusted or malformed definition is skipped loudly, not
    fatally: one bad file should not cost you the session.
    """
    directory = tools_dir(state_dir)
    if not os.path.isdir(directory):
        return []
    trusted = permissions.trusted_digests(state_dir)
    loaded = []
    for entry in sorted(os.listdir(directory)):
        if not entry.endswith(".json"):
            continue
        path = os.path.join(directory, entry)
        try:
            with open(path, "r", encoding="utf-8") as handle:
                definition = validate(json.load(handle), taken=registry.names())
        except (OSError, json.JSONDecodeError, ExtensionError) as error:
            out("[extension] skipped %s: %s" % (entry, error))
            continue
        key = "tool:%s" % definition["name"]
        if trusted.get(key) != permissions.policy_digest(path):
            out("[extension] %s wants to register %r:" % (entry, definition["name"]))
            out("    %s" % definition["description"])
            out("    $ %s" % definition["command"])
            if not interactive:
                out("[extension] skipped %s: not trusted and nobody to ask" % entry)
                continue
            if not permissions._default_trust_prompt("register this tool?"):
                out("[extension] skipped %s" % entry)
                continue
            permissions.mark_trusted(state_dir, path, key=key)
        registry.register(build_tool(definition, containment))
        loaded.append(definition["name"])
    return loaded


DEFINE_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "lowercase identifier, e.g. run_tests"},
        "description": {"type": "string",
                        "description": "what the tool does and when to reach for it"},
        "parameters": {"type": "object",
                       "description": "JSON Schema object describing the arguments"},
        "command": {"type": "string",
                    "description": "shell template; {arg} is replaced by the quoted argument"},
        "read_only": {"type": "boolean",
                      "description": "true only if the command cannot change anything"},
    },
    "required": ["name", "description", "command"],
}

DEFINE_TOOL_DESCRIPTION = (
    "Add a new tool to this harness, now. The tool is a shell command template: "
    "{arg} placeholders are replaced with the shell-quoted argument of that name, "
    "and every placeholder must be declared in parameters. The tool is available "
    "on the next step of this same turn, is written to .macroharness/tools/ so it "
    "survives the session, and crosses the permission policy on every call exactly "
    "like a built-in. Use it when you find yourself about to run the same shaped "
    "command a third time, or when the user describes a capability this harness "
    "does not have. It cannot shadow an existing tool."
)


def define_tool_tool(registry, containment, state_dir):
    """The tool that adds tools. Registration is immediate; the file is the record."""
    def define(name, description, command, parameters=None, read_only=False):
        try:
            definition = validate(
                {"name": name, "description": description, "command": command,
                 "parameters": parameters, "read_only": read_only},
                taken=registry.names(),
            )
        except ExtensionError as error:
            return "error: %s" % error
        path = save(state_dir, definition)
        registry.register(build_tool(definition, containment))
        return ("registered %r; it is callable from the next step.\n"
                "definition: %s\ncommand: %s" % (name, path, command))

    return Tool(DEFINE_TOOL_NAME, DEFINE_TOOL_DESCRIPTION, DEFINE_TOOL_SCHEMA,
                define, read_only=False)
