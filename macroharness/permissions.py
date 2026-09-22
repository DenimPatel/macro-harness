"""The permission policy: an ordered rule list in a JSON file, plus the prompt.

micro-harness's whole safety story is one y/n prompt. Here that prompt becomes a
small rule engine: rules are data, they live in the workspace, the user can read
them, and "always allow" writes a new one that is shown before it is saved.

Rule shape: {"tool": glob, "arg": glob, "verb": allow|ask|deny}. First match
wins, in file order. A rule of the form "cmd *" also matches the bare "cmd".
No matching rule denies: the shipped default ends with an explicit ask-all rule
so the fallback is visible in the file rather than hidden in code.
"""

import fnmatch
import hashlib
import json
import os

ALLOW, ASK, DENY = "allow", "ask", "deny"
VERBS = (ALLOW, ASK, DENY)
FILE_TOOLS = ("read_file", "write_file", "edit_file")
SHELL_METACHARACTERS = (";", "&", "|", ">", "`", "$(")

DEFAULT_RULES = [
    {"tool": "read_file", "arg": "*", "verb": ALLOW},
    {"tool": "run_bash", "arg": "ls *", "verb": ALLOW},
    {"tool": "run_bash", "arg": "cat *", "verb": ALLOW},
    {"tool": "run_bash", "arg": "rg *", "verb": ALLOW},
    {"tool": "run_bash", "arg": "grep *", "verb": ALLOW},
    {"tool": "run_bash", "arg": "find *", "verb": ALLOW},
    {"tool": "run_bash", "arg": "head *", "verb": ALLOW},
    {"tool": "run_bash", "arg": "tail *", "verb": ALLOW},
    {"tool": "run_bash", "arg": "wc *", "verb": ALLOW},
    {"tool": "run_bash", "arg": "git status *", "verb": ALLOW},
    {"tool": "run_bash", "arg": "git diff *", "verb": ALLOW},
    {"tool": "run_bash", "arg": "git log *", "verb": ALLOW},
    {"tool": "write_file", "arg": "*.env", "verb": DENY},
    {"tool": "write_file", "arg": "*.pem", "verb": DENY},
    {"tool": "*", "arg": "*", "verb": ASK},
]


class PolicyError(Exception):
    pass


class Decision:
    """The outcome of one authorization, kept so the log can explain itself."""

    def __init__(self, allowed, verb, rule=None, note=""):
        self.allowed = allowed
        self.verb = verb
        self.rule = rule
        self.note = note

    def __repr__(self):
        return "Decision(%s, %s)" % ("allow" if self.allowed else "deny", self.note)


def describe(rule):
    if rule is None:
        return "(no matching rule)"
    return '%s "%s" -> %s' % (rule.get("tool"), rule.get("arg"), rule.get("verb"))


def policy_path(state_dir):
    return os.path.join(state_dir, "policy.json")


def default_policy():
    return {"version": 1, "rules": [dict(rule) for rule in DEFAULT_RULES]}


def write_policy(path, policy):
    """Atomic, so a crash mid-write cannot leave an unparseable policy."""
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(policy, handle, indent=2)
        handle.write("\n")
    os.replace(temporary, path)


def ensure_policy(state_dir):
    path = policy_path(state_dir)
    if not os.path.exists(path):
        write_policy(path, default_policy())
    return path


def load_policy(path):
    """Strict on shape: a policy we cannot parse is a policy we do not run."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError) as error:
        raise PolicyError("cannot read %s: %s" % (path, error))
    if not isinstance(data, dict):
        raise PolicyError("%s must contain a JSON object" % path)
    rules = data.get("rules")
    if not isinstance(rules, list):
        raise PolicyError("%s must have a 'rules' list" % path)
    for index, rule in enumerate(rules):
        if not isinstance(rule, dict):
            raise PolicyError("rule %d must be an object" % index)
        if rule.get("verb") not in VERBS:
            raise PolicyError("rule %d has verb %r, expected one of %s"
                              % (index, rule.get("verb"), ", ".join(VERBS)))
        for key in ("tool", "arg"):
            if not isinstance(rule.get(key), str):
                raise PolicyError("rule %d needs a string %r" % (index, key))
    return data


def policy_digest(path):
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


def trusted_digests(state_dir):
    """Every workspace file this user has accepted, by key. Unreadable is empty."""
    try:
        with open(os.path.join(state_dir, "trusted.json"), "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def mark_trusted(state_dir, path, key="policy_sha256"):
    """Record one file's digest. Merges, because more than one file is trusted.

    Anything in the workspace that the harness will *execute* -- the policy, the
    hook list, the extension tools -- needs an entry here, or a cloned repository
    gets to run code you never saw.
    """
    trusted = trusted_digests(state_dir)
    trusted[key] = policy_digest(path)
    with open(os.path.join(state_dir, "trusted.json"), "w", encoding="utf-8") as handle:
        json.dump(trusted, handle, indent=2)


def _default_trust_prompt(question):
    try:
        answer = input("%s [y/N] " % question)
    except EOFError:
        return False
    return answer.strip().lower() in ("y", "yes")


def trust_policy(state_dir, path, prompt_fn=None, interactive=True, out=print):
    """Trust on first use.

    The policy file lives inside the workspace, so a cloned repository could
    ship a permissive one. Printing every rule once, before it is used, is the
    mitigation: silent trust is the thing to avoid, not trust itself.
    """
    digest = policy_digest(path)
    if trusted_digests(state_dir).get("policy_sha256") == digest:
        return
    rules = load_policy(path)["rules"]
    out("policy: %s defines %d rule(s):" % (path, len(rules)))
    for rule in rules:
        out("  %-5s %-30s %s" % (rule["verb"], rule["tool"], rule["arg"]))
    if not interactive:
        raise PolicyError("policy is not trusted and there is nobody to ask; "
                          "run once interactively to accept it")
    if not (prompt_fn or _default_trust_prompt)("trust this policy?"):
        raise PolicyError("policy not trusted")
    mark_trusted(state_dir, path)


def primary_arg(tool, arguments):
    """The one argument a rule matches on. Rules should not need arg positions."""
    if tool == "run_bash":
        return str(arguments.get("command") or "")
    if tool in FILE_TOOLS:
        return str(arguments.get("path") or "")
    return json.dumps(arguments, sort_keys=True)


def rule_matches(rule, tool, argument):
    if not fnmatch.fnmatchcase(tool, rule["tool"]):
        return False
    pattern = rule["arg"]
    if fnmatch.fnmatchcase(argument, pattern):
        return True
    # "git status *" should also cover a bare "git status".
    if pattern.endswith(" *") and argument == pattern[:-2]:
        return True
    return False


def derive_rule(tool, arguments):
    """Turn an "always allow" answer into the narrowest useful rule.

    Kept deliberately obvious, because the prompt prints exactly this rule
    before the user agrees to it.
    """
    if tool == "run_bash":
        command = str(arguments.get("command") or "").strip()
        if (not command or any(token in command for token in SHELL_METACHARACTERS)
                or " " not in command):
            pattern = command
        else:
            pattern = command.split()[0] + " *"
        return {"tool": tool, "arg": pattern, "verb": ALLOW}
    if tool in FILE_TOOLS:
        path = os.path.normpath(str(arguments.get("path") or ""))
        parent, name = os.path.split(path)
        extension = os.path.splitext(name)[1]
        if extension:
            pattern = os.path.join(parent, "*" + extension) if parent else "*" + extension
        else:
            pattern = path
        return {"tool": tool, "arg": pattern or "*", "verb": ALLOW}
    return {"tool": tool, "arg": "*", "verb": ALLOW}


def default_prompt(label, candidate):
    """y = once, a = write a rule (shown), d = deny. EOF is a denial."""
    while True:
        try:
            answer = input("approve %s?\n  [y] allow once   [a] always allow (%s)   [d] deny > "
                           % (label, describe(candidate)))
        except EOFError:
            return "d"
        answer = answer.strip().lower()
        if answer in ("y", "yes"):
            return "y"
        if answer in ("a", "always"):
            return "a"
        if answer in ("", "d", "n", "no"):
            return "d"


class Permissions:
    """Authorize one tool call, prompting when the policy says ask."""

    def __init__(self, policy, path, state_dir=None, interactive=True,
                 prompt_fn=None, out=print):
        self.policy = policy
        self.path = path
        self.state_dir = state_dir
        self.interactive = interactive
        self.prompt_fn = prompt_fn or default_prompt
        self.out = out

    @property
    def rules(self):
        return self.policy["rules"]

    def verb_for(self, tool, arguments):
        argument = primary_arg(tool, arguments)
        for rule in self.rules:
            if rule_matches(rule, tool, argument):
                return rule["verb"], rule
        return DENY, None

    def authorize(self, tool, arguments):
        verb, rule = self.verb_for(tool, arguments)
        if verb == ALLOW:
            return Decision(True, ALLOW, rule, "matched %s" % describe(rule))
        if verb == DENY:
            return Decision(False, DENY, rule, "denied by %s" % describe(rule))
        if not self.interactive:
            return Decision(False, ASK, rule,
                            "non-interactive: ask treated as deny")
        candidate = derive_rule(tool, arguments)
        answer = self.prompt_fn("%s %s" % (tool, primary_arg(tool, arguments)), candidate)
        if answer == "a":
            self.rules.insert(0, candidate)
            write_policy(self.path, self.policy)
            if self.state_dir:
                mark_trusted(self.state_dir, self.path)
            return Decision(True, ASK, candidate, "always allowed: wrote %s" % describe(candidate))
        if answer == "y":
            return Decision(True, ASK, rule, "allowed once by the user")
        return Decision(False, ASK, rule, "denied by the user")
