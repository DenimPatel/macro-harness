"""Replay: run recorded sessions again, with and without a proposed change.

Tier 1 (recorded, free, deterministic). A session is replayed in a scratch copy
of the workspace -- a `git worktree` at the commit the session recorded, or a
plain copy when there is no git history -- through a harness built exactly the
way `mh` builds one. The model is replaced by the recording: each turn's
assistant messages are fed back verbatim. Permission prompts are answered the
way the user answered them. Everything else runs for real: the policy, the
hooks, the tools, containment. Running it once with today's artifacts and once
with the candidate applied, and comparing tool call by tool call, shows exactly
what the candidate changes mechanically: prompts removed, calls newly denied or
blocked, errors introduced or fixed, hook output added.

What tier 1 cannot show is how a *model* would behave differently -- the
assistant messages are fixed. That is tier 2's job.

Tier 2 (live, opt-in, budgeted). A named replay-set case's prompt is run
against the real model in a scratch workspace, baseline and candidate, and
scored by the case's own check command, steps and tokens.

Limitations, stated rather than hidden: uncommitted changes at recording time
are not in the worktree; MCP servers are not started, so their calls fail the
same way on both sides; subagent (`task`) calls return their recorded result.
"""

import os
import re
import shutil
import subprocess
import tempfile

from .adapters import macroharness as adapter
from .apply import write_artifact
from .scripted import ScriptedModel
from .signals import answer_from_note


class Scratch:
    """A throwaway workspace at the commit a session was recorded against."""

    def __init__(self, workspace, git_head=None):
        self.workspace = workspace
        self.git_head = git_head
        self.base = None
        self.path = None
        self.worktree_root = None

    def _git(self, *args, cwd=None):
        return subprocess.run(["git"] + list(args), cwd=cwd or self.workspace,
                              capture_output=True, text=True, timeout=60)

    def __enter__(self):
        self.base = tempfile.mkdtemp(prefix="mh-replay-")
        target = os.path.join(self.base, "ws")
        if self.git_head and self._worktree(target):
            pass
        else:
            ignore_sessions = os.path.join(self.workspace, adapter.STATE_DIR)

            def ignore(directory, names):
                skipped = {".git"} & set(names)
                if os.path.realpath(directory) == os.path.realpath(ignore_sessions):
                    skipped |= {"sessions"} & set(names)
                return skipped

            shutil.copytree(self.workspace, target, ignore=ignore, symlinks=True)
            self.path = target
        adapter.copy_artifacts(self.workspace, self.path)
        adapter.trust_everything(self.path)
        return self

    def _worktree(self, target):
        try:
            prefix = self._git("rev-parse", "--show-prefix")
            if prefix.returncode != 0:
                return False
            if self._git("cat-file", "-e", self.git_head + "^{commit}").returncode != 0:
                return False
            added = self._git("worktree", "add", "--detach", target, self.git_head)
        except (OSError, subprocess.SubprocessError):
            return False
        if added.returncode != 0:
            return False
        self.worktree_root = target
        self.path = os.path.join(target, prefix.stdout.strip())
        os.makedirs(self.path, exist_ok=True)
        return True

    def __exit__(self, *_exc):
        if self.worktree_root:
            self._git("worktree", "remove", "--force", self.worktree_root)
        shutil.rmtree(self.base, ignore_errors=True)
        return False


def _recorded_task_results(turn):
    return [event.get("content") or "" for event in turn.tool_events
            if event.get("name") == "task"]


def replay_session(scratch_path, episode):
    """Replay every turn of one session. Returns per-call rows and per-turn ends."""
    model = ScriptedModel()
    answers = {}
    prompts = []
    task_results = []

    def answer(label):
        queue = answers.get(label)
        if queue:
            prompts.append({"label": label, "recorded": True})
            return queue.pop(0)
        prompts.append({"label": label, "recorded": False})
        return "y"  # a prompt the user never saw: count it, and let the call through

    harness = adapter.build_harness(scratch_path, model, answer_fn=answer)
    task = harness.registry.get("task")
    if task is not None:
        task.func = lambda **_kwargs: task_results.pop(0) if task_results else \
            "[replay: no recorded subagent result]"
    try:
        for turn in episode.turns:
            model.load(turn.assistant_messages)
            answers.clear()
            for event in turn.tool_events:
                if event.get("decision") == "ask":
                    label = "%s %s" % (event.get("name"), event.get("arg") or "")
                    answers.setdefault(label, []).append(
                        answer_from_note(event.get("note")) or "d")
            task_results[:] = _recorded_task_results(turn)
            harness.run_user_turn(turn.user_text)
    finally:
        harness.close()
    return _rows(harness.session.events()), prompts


def _rows(events):
    """Tool calls keyed by (turn, call id); turn ends by turn."""
    calls, ends = {}, {}
    turn = -1
    for event in events:
        kind = event.get("t")
        if kind == "user":
            turn += 1
        elif kind == "tool" and turn >= 0:
            calls[(turn, event.get("tool_call_id") or "")] = {
                "name": event.get("name"), "arg": event.get("arg"),
                "decision": event.get("decision"), "outcome": event.get("outcome"),
                "content": event.get("content") or ""}
        elif kind == "turn_end" and turn >= 0:
            ends[turn] = event.get("stopped")
    return {"calls": calls, "ends": ends}


def run_variant(workspace, episode, proposal=None):
    """Replay one session in a fresh scratch copy, optionally with `proposal` applied."""
    with Scratch(workspace, episode.header.get("git_head")) as scratch:
        if proposal is not None:
            write_artifact(scratch.path, proposal)
            adapter.trust_everything(scratch.path)
        rows, _prompts = replay_session(scratch.path, episode)
    return rows


EXIT = re.compile(r"^exit (-?\d+)")


def _exit_code(content):
    """`run_bash` and extension tools report `exit N`; None for anything else."""
    match = EXIT.match(content or "")
    return int(match.group(1)) if match else None


def compare(baseline, candidate, session_id):
    """What changed, call by call. Every list holds {session, turn} refs."""
    diff = {key: [] for key in ("asks_removed", "new_asks", "new_denials", "new_blocks",
                                "new_errors", "new_failures", "fixed_errors",
                                "changed_results", "stops_removed", "new_stops")}
    for key, before in baseline["calls"].items():
        after = candidate["calls"].get(key)
        ref = {"session": session_id, "turn": key[0]}
        if after is None:
            continue
        if before["decision"] == "ask" and after["decision"] != "ask":
            diff["asks_removed"].append(ref)
        if before["decision"] != "ask" and after["decision"] == "ask":
            diff["new_asks"].append(ref)
        for outcome, name in (("denied", "new_denials"), ("blocked", "new_blocks"),
                              ("error", "new_errors")):
            if after["outcome"] == outcome and before["outcome"] != outcome:
                diff[name].append(ref)
        if before["outcome"] == "error" and after["outcome"] == "ok":
            diff["fixed_errors"].append(ref)
        # A command that exited 0 and now exits non-zero is a failed call even
        # though the tool itself worked, so it is not an "error" outcome.
        if _exit_code(before["content"]) == 0 and (_exit_code(after["content"]) or 0) != 0:
            diff["new_failures"].append(ref)
        if before["content"] != after["content"]:
            diff["changed_results"].append(ref)
    for turn, stopped in baseline["ends"].items():
        after = candidate["ends"].get(turn)
        ref = {"session": session_id, "turn": turn}
        if stopped in ("max_steps", "budget") and after not in ("max_steps", "budget"):
            diff["stops_removed"].append(ref)
        if stopped not in ("max_steps", "budget") and after in ("max_steps", "budget"):
            diff["new_stops"].append(ref)
    return diff


def merge(diffs):
    merged = {}
    for diff in diffs:
        for key, refs in diff.items():
            merged.setdefault(key, []).extend(refs)
    return merged


class Tier1:
    """Replays sessions, caching baselines across proposals within one gate run."""

    def __init__(self, workspace):
        self.workspace = workspace
        self._baselines = {}

    def baseline(self, episode):
        if episode.id not in self._baselines:
            self._baselines[episode.id] = run_variant(self.workspace, episode)
        return self._baselines[episode.id]

    def diff(self, episodes, proposal):
        diffs = []
        for episode in episodes:
            candidate = run_variant(self.workspace, episode, proposal)
            diffs.append(compare(self.baseline(episode), candidate, episode.id))
        return merge(diffs)


# --- tier 2 ------------------------------------------------------------------------

def run_live(workspace, case, header, proposal, api_key_env, budget):
    """One live run of a replay-set case. Returns {passed, tokens, steps}."""
    with Scratch(workspace, header.get("git_head")) as scratch:
        if proposal is not None:
            write_artifact(scratch.path, proposal)
            adapter.trust_everything(scratch.path)
        base_url = header.get("base_url") or "https://api.deepseek.com"
        harness = adapter.build_harness(
            scratch.path, None, model_name=header.get("model") or "deepseek-chat",
            base_url=base_url, api_key_env=api_key_env, live=True)
        harness.budget = budget
        try:
            harness.run_user_turn(case["prompt"])
        finally:
            harness.close()
        passed = adapter.run_check(scratch.path, case["check"]) == 0 if case.get("check") \
            else None
        return {"passed": passed, "tokens": harness.accounting.total,
                "steps": harness.last_steps}


def tier2(workspace, cases, headers, proposal, api_key_env, budget, runs=1):
    """Baseline vs candidate on every case, until the token budget is spent."""
    spent = 0
    results = {"baseline": [], "candidate": [], "budget_exhausted": False}
    for name, case in sorted(cases.items()):
        for variant, candidate in (("baseline", None), ("candidate", proposal)):
            for _run in range(runs):
                remaining = budget - spent
                if remaining <= 0:
                    results["budget_exhausted"] = True
                    return results
                outcome = run_live(workspace, case, headers.get(case["session"], {}),
                                   candidate, api_key_env, remaining)
                outcome["case"] = name
                spent += outcome["tokens"]
                results[variant].append(outcome)
    # The last run can end past the budget; a budget that was overspent is not a pass.
    results["budget_exhausted"] = spent > budget
    return results
