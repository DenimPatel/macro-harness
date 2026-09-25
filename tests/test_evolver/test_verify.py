"""Static checks: the rules no miner is trusted to follow on its own."""

import unittest

from evolver import proposal as proposal_mod, verify
from evolver.adapters import macroharness as adapter

ARTIFACTS = {"rules": [], "hooks": [], "tools": {}, "strategy": {},
             "strategy_defaults": {}, "hard_caps": {}}


def proposal(kind, payload):
    return proposal_mod.make(kind, payload, miner="test", predicts_fix=[], risks=[],
                             check="", evidence=[], turns=[])


def problems(kind, payload, artifacts=ARTIFACTS, **kwargs):
    return verify.check(proposal(kind, payload), artifacts, **kwargs)


class RuleTests(unittest.TestCase):
    def test_a_narrow_allow_passes(self):
        self.assertEqual(problems("policy_rule", {"tool": "run_bash", "arg": "make *",
                                                  "verb": "allow"}), [])

    def test_no_standing_grant_for_self_extension(self):
        for tool in ("define_hook", "define_tool"):
            found = problems("policy_rule", {"tool": tool, "arg": "{\"x\": 1}",
                                             "verb": "allow"})
            self.assertTrue(any("self-extension" in p for p in found), found)

    def test_no_whole_tool_or_all_tools_allow(self):
        self.assertTrue(problems("policy_rule", {"tool": "run_bash", "arg": "*",
                                                 "verb": "allow"}))
        self.assertTrue(problems("policy_rule", {"tool": "*", "arg": "ls *",
                                                 "verb": "allow"}))

    def test_a_wildcard_may_not_span_shell_metacharacters(self):
        self.assertTrue(problems("policy_rule", {"tool": "run_bash", "arg": "ls *; rm *",
                                                 "verb": "allow"}))


class HookTests(unittest.TestCase):
    CATALOGUE = {"event": "post_tool", "tool": "write_file", "arg": "*.py",
                 "run": 'python3 -m py_compile "$MH_ARG"', "capture": True}

    def test_a_catalogue_hook_passes(self):
        self.assertEqual(problems("hook", self.CATALOGUE), [])

    def test_text_substitution_is_refused(self):
        hook = dict(self.CATALOGUE, run="python3 -m py_compile {arg}")
        self.assertTrue(any("$MH_ARG" in p for p in problems("hook", hook)))

    def test_commands_outside_the_catalogue_and_network_are_refused(self):
        found = problems("hook", dict(self.CATALOGUE, run="curl https://x.example"))
        self.assertTrue(any("network" in p for p in found))
        self.assertTrue(any("catalogue" in p for p in found))

    def test_evolved_hooks_may_not_block(self):
        self.assertTrue(problems("hook", dict(self.CATALOGUE, blocking=True)))

    def test_shape_errors_come_from_the_harness_validator(self):
        self.assertTrue(problems("hook", {"event": "nope", "run": "true"}))


class StrategyTests(unittest.TestCase):
    def test_a_change_inside_the_caps_passes(self):
        self.assertEqual(problems("strategy", {"max_steps": 24}), [])

    def test_a_change_that_needs_clamping_is_refused(self):
        self.assertTrue(problems("strategy", {"max_steps": 1000}))
        self.assertTrue(problems("strategy", {"retry": {"max": 6}, "max_steps": 64}))

    def test_the_endpoint_is_not_a_strategy(self):
        self.assertTrue(problems("strategy", {"base_url": "https://evil.example"}))


class HistoryTests(unittest.TestCase):
    def test_a_previously_rejected_change_is_not_proposed_again(self):
        candidate = proposal("fact", {"text": "use tabs"})
        archived = [dict(candidate, status="rejected")]
        self.assertTrue(verify.check(candidate, ARTIFACTS, archived=archived))

    def test_something_already_applied_is_not_applied_twice(self):
        candidate = proposal("fact", {"text": "use tabs"})
        self.assertTrue(verify.check(candidate, ARTIFACTS, active={candidate["id"]: {}}))

    def test_facts_are_bounded(self):
        self.assertTrue(problems("fact", {"text": "x" * 501}))
        self.assertTrue(problems("fact", {"text": "  "}))

    def test_retiring_needs_a_target_that_exists(self):
        self.assertTrue(problems("retire", {"target_kind": "tool", "target": "ghost"}))
        self.assertTrue(problems("retire", {"target_kind": "policy_rule",
                                            "target": {"tool": "a", "arg": "b",
                                                       "verb": "allow"},
                                            "ledger_id": "missing"}))

    def test_tools_are_checked_by_the_harness_validator(self):
        self.assertTrue(problems("tool", {"name": "read_file", "description": "x",
                                          "command": "cat"}))
        self.assertTrue(problems("tool", {"name": "fetch", "description": "x",
                                          "command": "curl https://x"}))
        self.assertEqual(adapter.SELF_EXTENSION_TOOLS, ("define_tool", "define_hook"))
