"""The same fixable error, again: propose a check that catches it at write time.

The fix is never free-form shell. It comes from a small catalogue of hooks a
human wrote once and can read in full: a syntax check that runs right after a
file of that type is written, with its output captured into the tool result so
the model sees the mistake in the same step it made it. The file path reaches
the hook only as the quoted `"$MH_ARG"` environment variable, never through
text substitution, so a file name cannot become a shell command.
"""

import fnmatch
import re

from ..proposal import evidence_from, make
from ..split import require_mining_set

# pattern of files, error text that shows up later, the hook that catches it
CATALOGUE = (
    {"name": "python-syntax", "glob": "*.py",
     "error": re.compile(r"SyntaxError|IndentationError|TabError"),
     "run": 'python3 -m py_compile "$MH_ARG"'},
    {"name": "json-syntax", "glob": "*.json",
     "error": re.compile(r"JSONDecodeError|Expecting (value|property name|',' delimiter)"),
     "run": 'python3 -m json.tool "$MH_ARG" > /dev/null'},
    {"name": "shell-syntax", "glob": "*.sh",
     "error": re.compile(r"syntax error near unexpected token|unexpected end of file"),
     "run": 'bash -n "$MH_ARG"'},
)
WRITE_TOOLS = ("write_file", "edit_file")


def catalogue_runs():
    return {entry["run"] for entry in CATALOGUE}


def _hits(mining, entry):
    """Turns where a matching file was written and the matching error followed."""
    hits = []
    for episode in mining:
        for turn in episode.turns:
            written_by = None
            for event in turn.tool_events:
                name = event.get("name")
                if name in WRITE_TOOLS and fnmatch.fnmatchcase(event.get("arg") or "",
                                                              entry["glob"]):
                    written_by = written_by or name
                elif written_by and entry["error"].search(event.get("content") or ""):
                    hits.append((turn, written_by))
                    break
    return hits


def mine(mining, context):
    require_mining_set(mining)
    proposals = []
    present = {(h.get("event"), h.get("tool"), h.get("arg"), h.get("run"))
               for h in context.artifacts["hooks"]}
    for entry in CATALOGUE:
        hits = _hits(mining, entry)
        if len(hits) < context.thresholds["failures_min"]:
            continue
        tools = [tool for _turn, tool in hits]
        tool = max(set(tools), key=tools.count)
        hook = {"event": "post_tool", "tool": tool, "arg": entry["glob"],
                "run": entry["run"], "capture": True}
        if (hook["event"], hook["tool"], hook["arg"], hook["run"]) in present:
            continue
        turns = [turn for turn, _tool in hits]
        proposals.append(make(
            "hook", hook, miner="failures:%s" % entry["name"],
            predicts_fix=[turn.ref for turn in turns],
            risks=["runs `%s` after every %s of %s; slower writes" % (entry["run"], tool,
                                                                      entry["glob"]),
                   "its output is shown to the model and adds to context"],
            check="tier 1: the hook fires on the predicted turns; no new errors, blocks "
                  "or denials on held-out sessions",
            evidence=evidence_from(turns, "%s error after writing %s" % (entry["name"],
                                                                         entry["glob"])),
            turns=turns,
            effect="run `%s` after every %s of a %s file and show its output to the model"
                   % (entry["run"], tool, entry["glob"]),
        ))
    return proposals
