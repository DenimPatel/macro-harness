"""Hooks: shell commands the harness runs at fixed points in the loop.

A tool is something the *model* decides to call. A hook is something the
*harness* runs whether the model likes it or not, at a point the model cannot
see. That difference is the whole reason hooks exist: it is where you put the
rules that must not be negotiable -- run the formatter after every write, refuse
any write to `main`, tell me when the turn is over.

    .macroharness/hooks.json
    {
      "version": 1,
      "hooks": [
        {"event": "pre_tool", "tool": "write_file", "arg": "*.py",
         "run": "test -w .", "blocking": true},
        {"event": "post_tool", "tool": "write_file", "arg": "*.py",
         "run": "python3 -m py_compile {path}", "capture": true},
        {"event": "turn_end", "run": "afplay /System/Library/Sounds/Glass.aiff"}
      ]
    }

Five events: `pre_tool`, `post_tool`, `tool_error`, `turn_end`, `session_end`.
`tool` and `arg` are the same globs `policy.json` uses, matched by the same
`permissions.rule_matches`, so there is one matching language in the harness and
not two.

Two of them can change what the model sees, which is what makes hooks more than
notifications:

- a `pre_tool` hook with `"blocking": true` that exits non-zero **stops the
  call**, and its output becomes the tool result. The model reads the refusal
  and adapts, the same way it reads any other tool failure.
- a `post_tool` hook with `"capture": true` **appends its stdout to the tool
  result**. A linter that runs after every write feeds its own errors back into
  the conversation without the model having thought to ask.

Hooks run through `tools.run_command`, so they inherit the workspace jail and
the scrubbed environment. Context arrives as `MH_*` variables and as the full
call as JSON on stdin. The file is re-read at the start of every turn, so a hook
added mid-session takes effect on the next turn without a restart -- and, being
executable code from the workspace, it re-crosses trust-on-first-use whenever it
changes.
"""

import json
import os

from . import permissions
from .tools import TIMED_OUT, Tool, run_command

PRE_TOOL = "pre_tool"
POST_TOOL = "post_tool"
TOOL_ERROR = "tool_error"
TURN_END = "turn_end"
SESSION_END = "session_end"
EVENTS = (PRE_TOOL, POST_TOOL, TOOL_ERROR, TURN_END, SESSION_END)

HOOK_TIMEOUT = 30
TRUST_KEY = "hooks_sha256"


class HookError(Exception):
    pass


def hooks_path(state_dir):
    return os.path.join(state_dir, "hooks.json")


def default_config():
    return {"version": 1, "hooks": []}


def validate(config):
    """Strict on shape, like the policy: a hook list we cannot parse is not run."""
    if not isinstance(config, dict):
        raise HookError("hooks.json must contain a JSON object")
    entries = config.get("hooks")
    if not isinstance(entries, list):
        raise HookError("hooks.json must have a 'hooks' list")
    clean = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise HookError("hook %d must be an object" % index)
        event = entry.get("event")
        if event not in EVENTS:
            raise HookError("hook %d has event %r, expected one of %s"
                            % (index, event, ", ".join(EVENTS)))
        run = entry.get("run")
        if not isinstance(run, str) or not run.strip():
            raise HookError("hook %d needs a non-empty 'run'" % index)
        clean.append({
            "event": event,
            "tool": str(entry.get("tool") or "*"),
            "arg": str(entry.get("arg") or "*"),
            "run": run,
            "blocking": bool(entry.get("blocking", False)),
            "capture": bool(entry.get("capture", False)),
            "timeout": int(entry.get("timeout") or HOOK_TIMEOUT),
        })
    return {"version": config.get("version", 1), "hooks": clean}


def write_config(path, config):
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(config, handle, indent=2)
        handle.write("\n")
    os.replace(temporary, path)


def ensure_config(state_dir):
    path = hooks_path(state_dir)
    if not os.path.exists(path):
        write_config(path, default_config())
    return path


def describe(hook):
    where = hook["event"]
    if hook["event"] in (PRE_TOOL, POST_TOOL, TOOL_ERROR):
        where = "%s %s %s" % (hook["event"], hook["tool"], hook["arg"])
    flags = " ".join(flag for flag in ("blocking", "capture") if hook[flag])
    return "%s -> %s%s" % (where, hook["run"], (" [%s]" % flags) if flags else "")


class Outcome:
    """What one hook did. `blocked` is the only field the loop must respect."""

    def __init__(self, blocked=False, note="", captured=""):
        self.blocked = blocked
        self.note = note
        self.captured = captured


class Hooks:
    """The loaded hook list, and the three call sites the loop uses.

    A harness with no hooks file gets an instance with an empty list, so the
    loop never branches on whether hooks exist.
    """

    def __init__(self, containment, config=None, path=None, state_dir=None,
                 interactive=True, out=print):
        self.containment = containment
        self.config = config or default_config()
        self.path = path
        self.state_dir = state_dir
        self.interactive = interactive
        self.out = out
        self._digest = None

    @property
    def hooks(self):
        return self.config["hooks"]

    # --- loading ------------------------------------------------------------

    @classmethod
    def load(cls, containment, state_dir, interactive=True, out=print):
        path = ensure_config(state_dir)
        instance = cls(containment, path=path, state_dir=state_dir,
                       interactive=interactive, out=out)
        instance.reload()
        return instance

    def reload(self):
        """Re-read the file if it changed, re-trusting it if it did.

        Re-prompting on change is the point, not an annoyance: a hook is a shell
        command this harness runs on its own initiative, so the digest that was
        accepted is the only version that may run. A hook the agent wrote through
        `define_hook` was approved by the permission prompt on the way in, and is
        marked trusted there, so it does not ask twice.
        """
        if not self.path or not os.path.exists(self.path):
            return False
        digest = permissions.policy_digest(self.path)
        if digest == self._digest:
            return False
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                config = validate(json.load(handle))
        except (OSError, json.JSONDecodeError, HookError) as error:
            self.out("[hooks] ignoring %s: %s" % (self.path, error))
            self.config = default_config()
            self._digest = digest
            return False
        if config["hooks"] and not self._trusted(digest, config):
            self.config = default_config()
            self._digest = digest
            return False
        self.config = config
        self._digest = digest
        return True

    def _trusted(self, digest, config):
        if permissions.trusted_digests(self.state_dir or "").get(TRUST_KEY) == digest:
            return True
        self.out("hooks: %s defines %d hook(s) this harness will run:"
                 % (self.path, len(config["hooks"])))
        for hook in config["hooks"]:
            self.out("  %s" % describe(hook))
        if not self.interactive:
            self.out("[hooks] not trusted and nobody to ask; running with no hooks")
            return False
        if not permissions._default_trust_prompt("trust these hooks?"):
            self.out("[hooks] not trusted; running with no hooks")
            return False
        permissions.mark_trusted(self.state_dir, self.path, key=TRUST_KEY)
        return True

    def trust_now(self):
        """Accept the file as it currently stands. Called after an approved write."""
        if self.path and self.state_dir and os.path.exists(self.path):
            permissions.mark_trusted(self.state_dir, self.path, key=TRUST_KEY)

    # --- matching and running ----------------------------------------------

    def matching(self, event, tool="", argument=""):
        found = []
        for hook in self.hooks:
            if hook["event"] != event:
                continue
            if event in (PRE_TOOL, POST_TOOL, TOOL_ERROR):
                if not permissions.rule_matches(hook, tool, argument):
                    continue
            found.append(hook)
        return found

    def _run(self, hook, payload):
        environment = {
            "MH_EVENT": hook["event"],
            "MH_TOOL": str(payload.get("tool") or ""),
            "MH_ARG": str(payload.get("arg") or ""),
        }
        for key in ("decision", "result", "reply"):
            if payload.get(key) is not None:
                environment["MH_" + key.upper()] = str(payload[key])[:4000]
        command = hook["run"]
        for placeholder, value in (("{tool}", environment["MH_TOOL"]),
                                   ("{arg}", environment["MH_ARG"])):
            command = command.replace(placeholder, value)
        return run_command(self.containment, command, extra_env=environment,
                           stdin_text=json.dumps(payload, default=str),
                           timeout=hook["timeout"])

    # --- the three call sites ----------------------------------------------

    def before_tool(self, tool, argument, arguments):
        """Returns an Outcome. `blocked` means do not run the tool."""
        payload = {"tool": tool, "arg": argument, "arguments": arguments}
        for hook in self.matching(PRE_TOOL, tool, argument):
            code, stdout, stderr = self._run(hook, payload)
            if code == 0:
                continue
            detail = (stderr.strip() or stdout.strip()
                      or "hook exited %d" % code)
            if hook["blocking"]:
                return Outcome(blocked=True, note="blocked by hook (%s): %s"
                               % (hook["run"], detail))
            self.out("  [hook] %s exited %d (not blocking)" % (hook["run"], code))
        return Outcome()

    def after_tool(self, tool, argument, arguments, result):
        """Returns an Outcome whose `captured` text is appended to the result."""
        payload = {"tool": tool, "arg": argument, "arguments": arguments,
                   "result": result}
        captured = []
        for hook in self.matching(POST_TOOL, tool, argument):
            code, stdout, stderr = self._run(hook, payload)
            if hook["capture"]:
                text = (stdout.strip() + ("\n" + stderr.strip() if stderr.strip() else "")).strip()
                if text:
                    captured.append("[hook %s exit %d]\n%s" % (hook["run"], code, text))
            elif code != 0:
                self.out("  [hook] %s exited %d" % (hook["run"], code))
        return Outcome(captured="\n".join(captured))

    def fire(self, event, **payload):
        """Notification-shaped events. Output is reported, never injected."""
        for hook in self.matching(event, payload.get("tool", ""), payload.get("arg", "")):
            code, _stdout, stderr = self._run(hook, payload)
            if code == TIMED_OUT:
                self.out("  [hook] %s timed out" % hook["run"])
            elif code != 0:
                self.out("  [hook] %s exited %d: %s" % (hook["run"], code, stderr.strip()[:200]))


DEFINE_HOOK_SCHEMA = {
    "type": "object",
    "properties": {
        "event": {"type": "string", "enum": list(EVENTS)},
        "run": {"type": "string", "description": "shell command; {tool} and {arg} are substituted"},
        "tool": {"type": "string", "description": "glob over tool names, default *"},
        "arg": {"type": "string", "description": "glob over the tool's primary argument, default *"},
        "blocking": {"type": "boolean",
                     "description": "pre_tool only: non-zero exit cancels the call"},
        "capture": {"type": "boolean",
                    "description": "post_tool only: stdout is appended to the tool result"},
    },
    "required": ["event", "run"],
}

DEFINE_HOOK_DESCRIPTION = (
    "Add a lifecycle hook to this harness, now. Events: pre_tool (before a matching "
    "tool call; with blocking=true a non-zero exit cancels it), post_tool (after; with "
    "capture=true the output is appended to the tool result), tool_error, turn_end and "
    "session_end. Use it for anything that should happen every time regardless of what "
    "this conversation is about: run the formatter after every write, refuse writes to a "
    "protected path, notify the user when a turn finishes. The hook is written to "
    ".macroharness/hooks.json and takes effect immediately."
)


def define_hook_tool(hooks):
    """The tool that adds hooks. Appends, reloads, and re-trusts in one step."""
    def define(event, run, tool="*", arg="*", blocking=False, capture=False):
        if not hooks.path:
            return "error: this harness has no hooks file"
        entry = {"event": event, "run": run, "tool": tool, "arg": arg,
                 "blocking": bool(blocking), "capture": bool(capture)}
        try:
            config = validate({"version": 1, "hooks": hooks.hooks + [entry]})
        except HookError as error:
            return "error: %s" % error
        write_config(hooks.path, config)
        hooks.config = config
        hooks._digest = permissions.policy_digest(hooks.path)
        hooks.trust_now()
        return "added hook: %s\nfile: %s" % (describe(config["hooks"][-1]), hooks.path)

    return Tool("define_hook", DEFINE_HOOK_DESCRIPTION, DEFINE_HOOK_SCHEMA,
                define, read_only=False)
