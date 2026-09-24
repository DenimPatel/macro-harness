"""The whole loop through `mh-evolve`: run, review, the next session, revert."""

import json
import os
from unittest import mock

from evolver import __main__ as evolve_cli
from evolver import apply as apply_mod, proposal as proposal_mod
from evolver.store import Store
from macroharness import __main__ as mh_cli
from macroharness import contract, permissions, strategy
from tests.fake_model import Collector, ScriptedModel
from tests.test_evolver.helpers import EvolverCase, ask_session, assistant, call

RULE = {"tool": "run_bash", "arg": "python3 *", "verb": "allow"}


def sid(number):
    return "20250101-0000%02d-s" % number


class EndToEndTests(EvolverCase):
    def evolve(self, *argv, answers=None):
        out = Collector()
        queue = list(answers or [])
        code = evolve_cli.main(["--workspace", self.root, "--state", self.state] + list(argv),
                               out=out, answer_fn=lambda _q: queue.pop(0) if queue else "s")
        return code, out.text

    def policy_rules(self):
        path = permissions.policy_path(os.path.join(self.root, ".macroharness"))
        return permissions.load_policy(path)["rules"]

    def new_session(self):
        args = mh_cli.build_parser().parse_args(["--workspace", self.root, "--non-interactive"])
        harness = mh_cli.build(args, model=ScriptedModel([
            assistant(tool_calls=[call("run_bash", {"command": "python3 -V"})]),
            assistant("ok")]), out=Collector())
        harness.run_user_turn("check python again")
        harness.close()
        return harness.session.events()

    def test_repeated_approvals_become_a_rule_that_the_next_session_uses(self):
        for number in range(1, 7):
            ask_session(self.root, sid(number))

        code, text = self.evolve("run")
        self.assertEqual(code, 0)
        store = Store(self.root, root=self.state)
        pending = {p["kind"]: p for p in store.inbox(status=proposal_mod.PENDING)}
        self.assertIn("policy_rule", pending)
        rule = pending["policy_rule"]
        self.assertEqual(rule["payload"], RULE)
        self.assertEqual(set(rule["provenance"]["sessions"]),
                         {sid(n) for n in (1, 3, 4, 5, 6)})
        held = rule["gate"]["tier1"]["held_out"]
        self.assertEqual((held["asks_removed"], held["new_errors"]), (1, 0))
        index = contract.read_evolved_index(os.path.join(self.root, ".macroharness"))
        self.assertEqual(index["inbox_pending"], len(pending))

        _code, text = self.evolve("review", "--accept-all-untainted")
        self.assertIn("applied.", text)
        self.assertEqual(self.policy_rules()[0], RULE)
        state = os.path.join(self.root, ".macroharness")
        self.assertEqual(permissions.trusted_digests(state)["policy_sha256"],
                         permissions.policy_digest(permissions.policy_path(state)))
        self.assertEqual(store.active_artifacts()[rule["id"]]["kind"], "policy_rule")

        events = self.new_session()
        tool = [e for e in events if e["t"] == "tool"][0]
        self.assertEqual((tool["decision"], tool["outcome"]), ("allow", "ok"))
        self.assertIn({"t": "artifact_use", "id": rule["id"]}, events)

        _code, text = self.evolve("stats")
        self.assertIn(rule["id"], text)

        code, _text = self.evolve("revert", rule["id"])
        self.assertEqual(code, 0)
        self.assertNotIn(RULE, self.policy_rules())
        self.assertNotIn(rule["id"], store.active_artifacts())
        index = contract.read_evolved_index(state)
        self.assertNotIn(rule["id"], index["artifacts"])

    def test_review_asks_and_a_rejection_is_remembered(self):
        for number in range(1, 7):
            ask_session(self.root, sid(number), text="check the python version")
        self.evolve("run")
        store = Store(self.root, root=self.state)
        count = len(store.inbox(status=proposal_mod.PENDING))
        _code, text = self.evolve("review", answers=["n"] * count)
        self.assertIn("rejected", text)
        self.assertEqual(store.inbox(status=proposal_mod.PENDING), [])
        _code, text = self.evolve("run")
        self.assertIn("rejected before", text)


class RememberingTests(EvolverCase):
    def evolve(self, *argv):
        out = Collector()
        evolve_cli.main(["--workspace", self.root, "--state", self.state] + list(argv),
                        out=out, answer_fn=lambda _q: "s")
        return out.text

    def test_a_gate_failure_is_retried_once_there_is_more_evidence(self):
        for number in (1, 3, 4, 5):  # none hashed out, so the newest is held out
            ask_session(self.root, sid(number), text="run the check")
        with mock.patch("evolver.gate.run", return_value={
                "passed": False, "reasons": ["forced"], "tier1": {}, "tier2": None}):
            self.assertIn("failed", self.evolve("run"))
        self.assertNotIn("mined policy-rule", self.evolve("run"))
        for number in (6, 7):
            ask_session(self.root, sid(number), text="run the check")
        self.assertIn("mined policy-rule", self.evolve("run"))

    def test_a_reverted_change_is_not_proposed_again(self):
        for number in range(1, 7):
            ask_session(self.root, sid(number))
        self.evolve("run")
        self.evolve("review", "--accept-all-untainted")
        store = Store(self.root, root=self.state)
        (rule_id,) = [i for i, e in store.active_artifacts().items()
                      if e["kind"] == "policy_rule"]
        self.evolve("revert", rule_id)
        text = self.evolve("run")
        self.assertIn("applied and then reverted", text)


class ReviewSafetyTests(EvolverCase):
    def test_accept_all_never_applies_a_tainted_proposal(self):
        store = Store(self.root, root=self.state)
        tainted = proposal_mod.make(
            "policy_rule", RULE, miner="test", predicts_fix=[], risks=[], check="",
            evidence=[], turns=[])
        tainted["provenance"]["tainted"] = True
        tainted["status"] = proposal_mod.PENDING
        store.put(tainted)
        out = Collector()
        from evolver.inbox import review
        summary = review(store, accept_all_untainted=True, out=out)
        self.assertEqual(summary["skipped"], [tainted["id"]])
        self.assertIn("WARNING", out.text)
        self.assertFalse(os.path.exists(os.path.join(self.root, ".macroharness",
                                                     "policy.json")))


class ApplyRevertTests(EvolverCase):
    def pending(self, kind, payload, **provenance):
        proposal = proposal_mod.make(kind, payload, miner=provenance.get("miner", "test"),
                                     predicts_fix=[], risks=[], check="", evidence=[],
                                     turns=[])
        proposal["status"] = proposal_mod.PENDING
        return proposal

    def test_a_strategy_change_applies_trusted_and_reverts_to_no_file(self):
        store = Store(self.root, root=self.state)
        os.makedirs(os.path.join(self.root, ".macroharness"))
        proposal = self.pending("strategy", {"max_steps": 24})
        apply_mod.apply(store, proposal)
        state = os.path.join(self.root, ".macroharness")
        settings = strategy.load(state, interactive=False, out=Collector())
        self.assertEqual(settings["max_steps"], 24)
        apply_mod.revert(store, proposal["id"])
        self.assertFalse(os.path.exists(strategy.strategy_path(state)))

    def test_retiring_an_applied_rule_and_reverting_the_retirement(self):
        store = Store(self.root, root=self.state)
        os.makedirs(os.path.join(self.root, ".macroharness"))
        rule = self.pending("policy_rule", RULE)
        apply_mod.apply(store, rule)
        retire = self.pending("retire", {"target_kind": "policy_rule", "target": RULE,
                                         "ledger_id": rule["id"], "reason": "unused"})
        apply_mod.apply(store, retire)
        rules = permissions.load_policy(permissions.policy_path(
            os.path.join(self.root, ".macroharness")))["rules"]
        self.assertNotIn(RULE, rules)
        self.assertNotIn(rule["id"], store.active_artifacts())
        apply_mod.revert(store, retire["id"])
        rules = permissions.load_policy(permissions.policy_path(
            os.path.join(self.root, ".macroharness")))["rules"]
        self.assertIn(RULE, rules)
        self.assertIn(rule["id"], store.active_artifacts())

    def test_a_fact_is_stored_outside_the_workspace(self):
        store = Store(self.root, root=self.state)
        fact = self.pending("fact", {"text": "use tabs", "scope": "project"})
        apply_mod.apply(store, fact)
        self.assertEqual(store.facts()[0]["text"], "use tabs")
        self.assertFalse(self.state.startswith(self.root))
        apply_mod.revert(store, fact["id"])
        self.assertEqual(store.facts(), [])

    def test_the_default_store_lives_outside_the_workspace(self):
        store = Store(self.root)
        self.addCleanup(lambda: __import__("shutil").rmtree(store.root, ignore_errors=True))
        self.assertFalse(os.path.realpath(store.root).startswith(self.root))


class ReplaySetTests(EvolverCase):
    def evolve(self, *argv):
        out = Collector()
        code = evolve_cli.main(["--workspace", self.root, "--state", self.state] + list(argv),
                               out=out)
        return code, out.text

    def test_cases_must_come_from_held_out_sessions(self):
        for number in range(1, 4):
            ask_session(self.root, sid(number))
        code, text = self.evolve("replayset", "add", "%s:0" % sid(1), "--check", "true")
        self.assertEqual(code, 1)
        self.assertIn("mining set", text)
        code, _text = self.evolve("replayset", "add", "%s:0" % sid(2), "--check", "true",
                                  "--name", "py")
        self.assertEqual(code, 0)
        _code, text = self.evolve("replayset", "list")
        self.assertIn("py", text)
        cases = Store(self.root, root=self.state).replayset()
        self.assertEqual(cases["py"]["prompt"], "check the python version for %s" % sid(2))
        self.assertEqual(json.loads(json.dumps(cases))["py"]["check"], "true")
