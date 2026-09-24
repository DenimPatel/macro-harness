"""Miners: patterns in the mining set become proposals, and nothing else does."""

import json
import os
from unittest import mock

from evolver import miners, pipeline, split, traces
from evolver.adapters import macroharness as adapter
from evolver.miners import approvals, failures, restatement, retire, strategy
from evolver.store import Store
from macroharness.session import Session
from tests.test_evolver.helpers import EvolverCase, ask_session, assistant, call, record


def sid(number, year="2025"):
    return "%s0101-0000%02d-s" % (year, number)


def context(root, **thresholds):
    return miners.Context(adapter.current_artifacts(root), thresholds=thresholds)


def mining_set(root):
    mining, _held = split.split(traces.load(root))
    return mining


class ApprovalMinerTests(EvolverCase):
    def test_repeated_approvals_become_the_narrow_rule(self):
        # Four sessions: with three, the newest would be held out as a fallback.
        for number in (1, 3, 4, 5):
            ask_session(self.root, sid(number))
        (proposal,) = approvals.mine(mining_set(self.root), context(self.root))
        self.assertEqual(proposal["payload"],
                         {"tool": "run_bash", "arg": "python3 *", "verb": "allow"})
        self.assertEqual(len(proposal["contract"]["predicts_fix"]), 3)
        self.assertNotIn(sid(5), proposal["provenance"]["sessions"])
        self.assertFalse(proposal["provenance"]["tainted"])

    def test_denials_and_thin_evidence_propose_nothing(self):
        ask_session(self.root, sid(1))
        ask_session(self.root, sid(3), answer="d")
        ask_session(self.root, sid(4), answer="d")
        self.assertEqual(approvals.mine(mining_set(self.root), context(self.root)), [])

    def test_one_session_is_not_enough(self):
        record(self.root, sid(1), [("go %d" % n, [
            assistant(tool_calls=[call("run_bash", {"command": "python3 -V"})]),
            assistant("ok")], ["y"]) for n in range(4)])
        self.assertEqual(approvals.mine(mining_set(self.root), context(self.root)), [])

    def test_self_extension_is_never_proposed(self):
        for number in (1, 3, 4):
            record(self.root, sid(number), [("add a tool", [
                assistant(tool_calls=[call("define_hook", {"event": "turn_end", "run": "true"})]),
                assistant("ok")], ["y"])])
        self.assertEqual(approvals.mine(mining_set(self.root), context(self.root)), [])

    def test_miners_refuse_anything_but_the_mining_set(self):
        ask_session(self.root, sid(1))
        with self.assertRaises(TypeError):
            approvals.mine(traces.load(self.root), context(self.root))


class HeldOutIsHiddenTests(EvolverCase):
    def test_no_miner_ever_receives_a_held_out_session(self):
        for number in range(1, 7):
            ask_session(self.root, sid(number))
        seen = set()

        def spy(mining, _context):
            seen.update(episode.id for episode in mining)
            return []

        patches = [mock.patch.object(module, "mine", side_effect=spy)
                   for module in miners.MINERS]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        pipeline.mine(Store(self.root, root=self.state), traces.load(self.root),
                      out=lambda *_a: None)
        self.assertTrue(seen)
        self.assertNotIn(sid(2), seen)
        self.assertTrue(split.is_held_out(sid(2)))


def syntax_error_session(root, session_id):
    return record(root, session_id, [("write the script", [
        assistant(tool_calls=[call("write_file", {"path": "a.py", "content": "def f(:\n"})]),
        assistant(tool_calls=[call("run_bash", {"command": "python3 a.py"})]),
        assistant("it failed"),
    ], ["y", "y"])])


class FailureMinerTests(EvolverCase):
    def test_a_recurring_syntax_error_proposes_the_catalogue_hook(self):
        for number in (1, 3):
            syntax_error_session(self.root, sid(number))
        (proposal,) = failures.mine(mining_set(self.root), context(self.root))
        self.assertEqual(proposal["payload"], {
            "event": "post_tool", "tool": "write_file", "arg": "*.py",
            "run": 'python3 -m py_compile "$MH_ARG"', "capture": True})

    def test_one_occurrence_is_not_a_pattern(self):
        syntax_error_session(self.root, sid(1))
        self.assertEqual(failures.mine(mining_set(self.root), context(self.root)), [])


class StrategyMinerTests(EvolverCase):
    def test_turns_that_run_out_of_steps_raise_max_steps(self):
        spin = [assistant(tool_calls=[call("run_bash", {"command": "true"})])] * 2
        for number in (1, 3):
            record(self.root, sid(number), [("spin", spin, ["y", "y"])], max_steps=2)
        (proposal,) = strategy.mine(mining_set(self.root), context(self.root))
        self.assertEqual(proposal["payload"], {"max_steps": 25})
        self.assertEqual(proposal["provenance"]["miner"], "strategy:max_steps")

    def test_exhausted_retries_propose_one_more(self):
        sessions = os.path.join(self.root, ".macroharness", "sessions")
        for number in (1, 3):
            session = Session.at(sessions, sid(number))
            for event in ({"t": "system", "text": "s"}, {"t": "header"},
                          {"t": "user", "text": "hi"},
                          {"t": "retry", "status": 529, "attempt": 3},
                          {"t": "assistant", "message": {"role": "assistant", "content": "x"}},
                          {"t": "turn_end", "steps": 1, "tokens": 5, "stopped": "none"}):
                session.append(event)
        (proposal,) = strategy.mine(mining_set(self.root), context(self.root))
        self.assertEqual(proposal["payload"], {"retry": {"max": 4}})


class RestatementMinerTests(EvolverCase):
    def test_a_repeated_instruction_becomes_a_fact(self):
        for number in (1, 3, 4, 5):
            record(self.root, sid(number), [("always run the linter before committing",
                                             [assistant("ok")], None)])
        (proposal,) = restatement.mine(mining_set(self.root), context(self.root))
        self.assertEqual(proposal["kind"], "fact")
        self.assertIn("linter", proposal["payload"]["text"])


class RetireMinerTests(EvolverCase):
    def test_an_applied_artifact_nobody_uses_is_proposed_for_retirement(self):
        for number in (2, 4, 5, 7):  # none hashed out; 7 is held out as the newest
            record(self.root, sid(number, "2099"), [("hi", [assistant("ok")], None)])
        active = {"policy-rule-x": {"id": "policy-rule-x", "kind": "policy_rule",
                                    "payload": {"tool": "run_bash", "arg": "make *",
                                                "verb": "allow"},
                                    "applied_at": "20250101-000000", "model": "scripted"}}
        ctx = miners.Context(adapter.current_artifacts(self.root), active=active,
                             thresholds={"retire_unused_sessions": 3})
        (proposal,) = retire.mine(mining_set(self.root), ctx)
        self.assertEqual(proposal["payload"]["ledger_id"], "policy-rule-x")

    def test_a_model_change_retires_what_was_learned_on_the_old_model(self):
        for number in (2, 4, 5, 7):  # none hashed out; 7 is held out as the newest
            record(self.root, sid(number, "2099"), [("hi", [assistant("ok")], None)])
        active = {"hook-y": {"id": "hook-y", "kind": "hook",
                             "payload": {"event": "turn_end", "run": "true"},
                             "applied_at": "20250101-000000", "model": "old-model"}}
        with mock.patch("evolver.traces.Episode.model", new_callable=mock.PropertyMock,
                        return_value="new-model"):
            ctx = miners.Context(adapter.current_artifacts(self.root), active=active,
                                 thresholds={"retire_model_sessions": 3})
            (proposal,) = retire.mine(mining_set(self.root), ctx)
        self.assertIn("learned under old-model", proposal["payload"]["reason"])

    def test_an_unused_self_defined_tool_is_proposed_for_removal(self):
        tools = os.path.join(self.root, ".macroharness", "tools")
        os.makedirs(tools)
        with open(os.path.join(tools, "old_tool.json"), "w") as handle:
            json.dump({"name": "old_tool", "description": "d", "command": "true"}, handle)
        os.utime(os.path.join(tools, "old_tool.json"), (0, 0))
        for number in (1, 3, 4, 5):
            record(self.root, sid(number), [("hi", [assistant("ok")], None)])
        (proposal,) = retire.mine(mining_set(self.root),
                                  context(self.root, retire_unused_sessions=3))
        self.assertEqual(proposal["payload"]["target"], "old_tool")
