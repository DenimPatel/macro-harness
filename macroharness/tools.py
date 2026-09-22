"""The tool registry, the built-in tools, and workspace containment.

micro-harness ships three tools and hardcodes them. macro-harness keeps the same
three (plus structured editing) but hangs them off a registry, because MCP tools
and subagents both need to add to the same list and flow through the same
permission pipeline.
"""

import json
import os
import signal
import subprocess

MAX_TOOL_OUTPUT = 8000
ENV_ALLOW = ("PATH", "HOME", "LANG", "LC_ALL", "TERM", "TMPDIR")


def truncate(text, limit=MAX_TOOL_OUTPUT):
    """Keep the head and tail; one unbounded `cat` can end a session."""
    if len(text) <= limit:
        return text
    half = limit // 2
    return "%s\n... [%d characters truncated] ...\n%s" % (
        text[:half], len(text) - limit, text[-half:],
    )


def decode_args(raw):
    """Model-authored arguments arrive as a JSON string. Failures are data."""
    try:
        arguments = json.loads(raw or "{}")
    except json.JSONDecodeError as error:
        return None, "invalid JSON arguments: %s" % error
    if not isinstance(arguments, dict):
        return None, "arguments must be a JSON object"
    return arguments, None


class Tool:
    def __init__(self, name, description, parameters, func, read_only=False):
        self.name = name
        self.description = description
        self.parameters = parameters
        self.func = func
        self.read_only = read_only

    def schema(self):
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class Registry:
    """One place to add a tool: a schema, a function, and whether it writes."""

    def __init__(self):
        self._tools = {}

    def register(self, tool):
        self._tools[tool.name] = tool
        return tool

    def get(self, name):
        return self._tools.get(name)

    def schemas(self):
        return [tool.schema() for tool in self._tools.values()]

    def names(self):
        return list(self._tools)


class Containment:
    """A workspace jail. Containment, not isolation: see README.

    Every path is resolved with realpath before use, so `..` and symlinks are
    both refused. This check runs *after* the policy, so no rule can authorize
    escaping the workspace root.
    """

    def __init__(self, root, timeout=60, env_allow=()):
        self.root = os.path.realpath(root)
        self.timeout = timeout
        self.env_allow = tuple(env_allow)

    def resolve(self, path):
        candidate = os.path.realpath(os.path.join(self.root, str(path or "")))
        if candidate == self.root or candidate.startswith(self.root + os.sep):
            return candidate, None
        return None, "error: path escapes the workspace: %s" % path

    def env(self):
        env = {key: os.environ[key] for key in ENV_ALLOW if key in os.environ}
        for key in self.env_allow:
            if key in os.environ:
                env[key] = os.environ[key]
        return env


def _read_file(containment, path, offset=0, limit=None):
    full, error = containment.resolve(path)
    if error:
        return error
    with open(full, "r", encoding="utf-8", errors="replace") as handle:
        lines = handle.readlines()
    start = max(int(offset or 0), 0)
    if limit in (None, 0):
        chunk = lines[start:]
    else:
        chunk = lines[start:start + int(limit)]
    if not chunk and start:
        return "error: offset %d is past the end of %s" % (start, path)
    return "".join(chunk)


def _write_file(containment, path, content):
    full, error = containment.resolve(path)
    if error:
        return error
    parent = os.path.dirname(full)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(full, "w", encoding="utf-8") as handle:
        handle.write(content)
    return "wrote %d bytes to %s" % (len(content.encode()), path)


def _edit_file(containment, path, old_string, new_string, replace_all=False):
    full, error = containment.resolve(path)
    if error:
        return error
    if not old_string:
        return "error: old_string must not be empty"
    with open(full, "r", encoding="utf-8") as handle:
        text = handle.read()
    occurrences = text.count(old_string)
    if occurrences == 0:
        return "error: old_string not found in %s" % path
    if occurrences > 1 and not replace_all:
        return ("error: old_string appears %d times in %s; include more context "
                "or pass replace_all=true" % (occurrences, path))
    with open(full, "w", encoding="utf-8") as handle:
        handle.write(text.replace(old_string, new_string))
    return "replaced %d occurrence(s) in %s" % (occurrences if replace_all else 1, path)


TIMED_OUT = -1


def run_command(containment, command, extra_env=None, stdin_text=None, timeout=None):
    """Run one contained shell command. Returns (returncode, stdout, stderr).

    The single place a subprocess is started, so `run_bash`, hooks and
    extension tools all inherit the same jail: the workspace as cwd, a scrubbed
    environment, a timeout, and a killed process group on the way out.
    `extra_env` is layered on top of the scrubbed environment, never under it,
    so a caller can pass context in without widening what the child inherits.
    """
    limit = containment.timeout if timeout is None else timeout
    env = containment.env()
    env.update(extra_env or {})
    kwargs = {}
    if os.name != "nt":
        kwargs["start_new_session"] = True
    process = subprocess.Popen(
        command, shell=True, cwd=containment.root, env=env,
        stdin=subprocess.PIPE if stdin_text is not None else None,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, **kwargs
    )
    try:
        stdout, stderr = process.communicate(input=stdin_text, timeout=limit)
    except subprocess.TimeoutExpired:
        if os.name != "nt":
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        else:
            process.kill()
        process.communicate()
        return TIMED_OUT, "", "timed out after %ds" % limit
    return process.returncode, stdout, stderr


def _run_bash(containment, command):
    code, stdout, stderr = run_command(containment, command)
    if code == TIMED_OUT:
        return "error: command timed out after %ds" % containment.timeout
    return "exit %d\nstdout:\n%s\nstderr:\n%s" % (code, stdout, stderr)


def default_registry(containment):
    registry = Registry()
    registry.register(Tool(
        "read_file",
        "Read a text file from the workspace and return its contents.",
        {"type": "object", "properties": {
            "path": {"type": "string"},
            "offset": {"type": "integer", "description": "first line to return (0-based)"},
            "limit": {"type": "integer", "description": "how many lines to return"},
        }, "required": ["path"]},
        lambda **kwargs: _read_file(containment, **kwargs),
        read_only=True,
    ))
    registry.register(Tool(
        "write_file",
        "Write text to a file in the workspace, replacing it.",
        {"type": "object", "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"},
        }, "required": ["path", "content"]},
        lambda **kwargs: _write_file(containment, **kwargs),
    ))
    registry.register(Tool(
        "edit_file",
        "Replace an exact string in a file. Fails unless the match is unique.",
        {"type": "object", "properties": {
            "path": {"type": "string"},
            "old_string": {"type": "string"},
            "new_string": {"type": "string"},
            "replace_all": {"type": "boolean"},
        }, "required": ["path", "old_string", "new_string"]},
        lambda **kwargs: _edit_file(containment, **kwargs),
    ))
    registry.register(Tool(
        "run_bash",
        "Run a shell command in the workspace and return exit code, stdout and stderr.",
        {"type": "object", "properties": {"command": {"type": "string"}},
         "required": ["command"]},
        lambda **kwargs: _run_bash(containment, **kwargs),
    ))
    return registry
