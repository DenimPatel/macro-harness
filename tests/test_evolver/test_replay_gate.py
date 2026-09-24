"""Tier 1 replay and the gate: what a change does, measured, before anyone accepts it."""

import os
import subprocess
from unittest import mock

from evolver import gate, proposal as proposal_mod, replay, split, traces
from evolver.adapters import macroharness as adapter
from evolver.miners import failures
from tests.test_evolver.helpers import EvolverCase, ask_session, assistant, call, record


def sid(number):
    return "20250101-0000%02d-s" % number


def rule_proposal(verb, refs):
    return proposal_mod.make(
        "policy_rule", {"tool": "run_bash", "arg": "python3 *", "verb": verb},
        miner="test", predicts_fix=refs, risks=[], check="", evidence=[], turns=[])


class ReplayTests(EvolverCase):
    def test_a_replay_of_the_baseline_reproduces_the_recording(self):
        ask_session(self.root, sid(1))
        (episode,) = traces.load(self.root)
        rows = replay.run_variant(self.root, episode)
        ((key, call_row),) = rows["calls"].items()
        self.assertEqual(call_row["decision"], "ask")
        self.assertEqual(call_row["outcome"], "ok")
        self.assertIn("Python", call_row["content"])
        self.assertEqual(rows["ends"], {0: "none"})

    def test_a_candidate_rule_shows_up_as_prompts_removed_and_nothing_else(self):
        ask_session(self.root, sid(1))
        (episode,) = traces.load(self.root)
        diff = replay.Tier1(self.root).diff([episode], rule_proposal("allow", []))
        self.assertEqual(len(diff["asks_removed"]), 1)
        for key in ("new_errors", "new_blocks", "new_denials", "changed_results"):
            self.assertEqual(diff[key], [], key)

    def test_a_deny_candidate_shows_up_as_a_new_denial(self):
        ask_session(self.root, sid(1))
        (episode,) = traces.load(self.root)
        diff = replay.Tier1(self.root).diff([episode], rule_proposal("deny", []))
        self.assertEqual(len(diff["new_denials"]), 1)

    def test_replay_does_not_touch_the_workspace(self):
        record(self.root, sid(1), [("write", [
            assistant(tool_calls=[call("write_file", {"path": "made.txt", "content": "x"})]),
            assistant("ok")], ["y"])])
        os.remove(os.path.join(self.root, "made.txt"))
        before = sorted(os.listdir(os.path.join(self.root, ".macroharness", "sessions")))
        (episode,) = traces.load(self.root)
        replay.run_variant(self.root, episode, rule_proposal("allow", []))
        self.assertFalse(os.path.exists(os.path.join(self.root, "made.txt")))
        self.assertEqual(sorted(os.listdir(os.path.join(self.root, ".macroharness",
                                                        "sessions"))), before)

    def test_the_scratch_is_a_worktree_at_the_recorded_commit(self):
        def git(*args):
            subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t"] + list(args),
                           cwd=self.root, check=True, capture_output=True)
        git("init", "-q")
        with open(os.path.join(self.root, "tracked.txt"), "w") as handle:
            handle.write("committed")
        git("add", "tracked.txt")
        git("commit", "-qm", "one")
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.root,
                              capture_output=True, text=True).stdout.strip()
        with open(os.path.join(self.root, "tracked.txt"), "w") as handle:
            handle.write("changed later")
        with replay.Scratch(self.root, head) as scratch:
            with open(os.path.join(scratch.path, "tracked.txt")) as handle:
                self.assertEqual(handle.read(), "committed")
            self.assertTrue(os.path.exists(os.path.join(scratch.path, ".macroharness",
                                                        "mcp.json")))
        listing = subprocess.run(["git", "worktree", "list"], cwd=self.root,
                                 capture_output=True, text=True).stdout
        self.assertEqual(len(listing.strip().splitlines()), 1)


class GateTests(EvolverCase):
    def sessions(self, numbers, **kwargs):
        for number in numbers:
            ask_session(self.root, sid(number), **kwargs)
        return split.split(traces.load(self.root))

    def test_an_allow_rule_passes_with_its_predicted_effect(self):
        mining, held = self.sessions(range(1, 7))
        refs = [{"session": sid(n), "turn": 0} for n in (1, 3, 4)]
        result = gate.run(rule_proposal("allow", refs), mining, held,
                          gate.make_tier1(self.root))
        self.assertTrue(result["passed"], result["reasons"])
        self.assertEqual(result["tier1"]["predicted"]["hits"], 3)
        self.assertEqual(result["tier1"]["held_out"]["asks_removed"], 1)

    def test_a_candidate_that_newly_denies_a_held_out_call_fails(self):
        mining, held = self.sessions(range(1, 7))
        refs = [{"session": sid(1), "turn": 0}]
        deny = proposal_mod.make(
            "retire", {"target_kind": "policy_rule", "target": {}, "ledger_id": None},
            miner="test", predicts_fix=refs, risks=[], check="", evidence=[], turns=[])
        with mock.patch("evolver.replay.write_artifact",
                        lambda path, _p: adapter.add_rule(
                            path, {"tool": "run_bash", "arg": "python3 *", "verb": "deny"})):
            result = gate.run(deny, mining, held, gate.make_tier1(self.root))
        self.assertFalse(result["passed"])
        self.assertTrue(any("new denials" in reason for reason in result["reasons"]))

    def test_a_command_that_newly_exits_non_zero_is_a_regression(self):
        def rows(content):
            return {"calls": {(0, "c"): {"name": "run_bash", "arg": "x", "decision": "allow",
                                         "outcome": "ok", "content": content}}, "ends": {}}

        diff = replay.compare(rows("exit 0\nstdout:\n"), rows("exit 2\nstdout:\n"), "s")
        self.assertEqual(len(diff["new_failures"]), 1)
        self.assertEqual(diff["new_errors"], [])
        self.assertIn("new_failures", gate.REGRESSIONS)

    def test_no_prediction_hit_is_a_failure(self):
        mining, held = self.sessions(range(1, 7), command="ls -la")
        refs = [{"session": sid(1), "turn": 0}]
        result = gate.run(rule_proposal("allow", refs), mining, held,
                          gate.make_tier1(self.root))
        self.assertFalse(result["passed"])
        self.assertTrue(any("predicted" in reason for reason in result["reasons"]))

    def test_without_held_out_sessions_nothing_passes(self):
        mining, held = self.sessions((1, 3))
        self.assertEqual(list(held), [])
        result = gate.run(rule_proposal("allow", []), mining, held,
                          gate.make_tier1(self.root))
        self.assertFalse(result["passed"])

    def test_a_catalogue_hook_is_seen_firing_on_the_turns_it_claims_to_fix(self):
        for number in range(1, 7):
            record(self.root, sid(number), [("write the script", [
                assistant(tool_calls=[call("write_file",
                                           {"path": "a.py", "content": "def f(:\n"})]),
                assistant(tool_calls=[call("run_bash", {"command": "python3 a.py"})]),
                assistant("it failed")], ["y", "y"])])
        mining, held = split.split(traces.load(self.root))
        from evolver import miners
        (hook,) = failures.mine(mining, miners.Context(adapter.current_artifacts(self.root)))
        result = gate.run(hook, mining, held, gate.make_tier1(self.root))
        self.assertTrue(result["passed"], result["reasons"])
        self.assertGreater(result["tier1"]["predicted"]["hits"], 0)


class Tier2Tests(EvolverCase):
    def test_scores_compare_pass_rate_and_tokens(self):
        outcome = {"budget_exhausted": False,
                   "baseline": [{"passed": True, "tokens": 100, "steps": 2}],
                   "candidate": [{"passed": False, "tokens": 200, "steps": 3}]}
        scored = gate._score(outcome)
        self.assertEqual(len(scored["reasons"]), 2)

    def test_a_budget_that_runs_out_is_not_a_pass(self):
        calls = []

        def fake_run(_ws, case, _header, proposal, _env, budget):
            calls.append((case["prompt"], proposal is None, budget))
            return {"passed": True, "tokens": 60, "steps": 1}

        cases = {"a": {"session": "s", "prompt": "p", "check": "true"}}
        with mock.patch("evolver.replay.run_live", fake_run):
            outcome = replay.tier2(self.root, cases, {}, {"kind": "x"}, "KEY", budget=100)
        self.assertTrue(outcome["budget_exhausted"])
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1][2], 40)
        self.assertTrue(gate._score(outcome)["reasons"])

    def test_live_runs_use_the_recorded_model_and_the_case_check(self):
        ask_session(self.root, sid(1))
        (episode,) = traces.load(self.root)
        from evolver.scripted import ScriptedModel
        built = {}
        original = adapter.build_harness

        def fake_build(path, _model, **kwargs):
            built.update(kwargs)
            return original(path, ScriptedModel([assistant(
                tool_calls=[call("write_file", {"path": "done.txt", "content": "x"})]),
                assistant("ok")]), answer_fn=lambda _label: "y")

        case = {"session": sid(1), "prompt": "make done.txt", "check": "test -f done.txt"}
        with mock.patch.object(adapter, "build_harness", fake_build):
            outcome = replay.run_live(self.root, case, episode.header, None, "KEY", 10_000)
        self.assertTrue(outcome["passed"])
        self.assertTrue(built["live"])
        self.assertEqual(built["api_key_env"], "KEY")
