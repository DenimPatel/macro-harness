"""The policy engine: precedence, the three-way prompt, and safe persistence."""

import json
import os
import tempfile
import unittest

from macroharness import permissions
from macroharness.permissions import ALLOW, ASK, DENY, PolicyError, Permissions
from tests.fake_model import assistant, build_harness, call, load_json

ASK_ALL = [{"tool": "*", "arg": "*", "verb": ASK}]


def console(rules, path=None, state_dir=None, interactive=True, answer=None):
    """A Permissions object whose prompt fails the test if it is never expected."""
    def prompt_fn(_label, _candidate):
        if answer is None:
            raise AssertionError("the policy should not have prompted")
        return answer

    policy = {"version": 1, "rules": [dict(rule) for rule in rules]}
    return policy, Permissions(policy, path, state_dir=state_dir,
                               interactive=interactive, prompt_fn=prompt_fn)


class PrecedenceTests(unittest.TestCase):
    def test_first_matching_rule_wins(self):
        _policy, permissions_ = console([
            {"tool": "run_bash", "arg": "git *", "verb": DENY},
            {"tool": "run_bash", "arg": "*", "verb": ALLOW},
        ], answer=None)
        decision = permissions_.authorize("run_bash", {"command": "git push"})
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.verb, DENY)

    def test_later_rule_applies_when_the_first_does_not_match(self):
        _policy, permissions_ = console([
            {"tool": "run_bash", "arg": "git *", "verb": DENY},
            {"tool": "run_bash", "arg": "*", "verb": ALLOW},
        ], answer=None)
        self.assertTrue(permissions_.authorize("run_bash", {"command": "ls -la"}).allowed)

    def test_no_matching_rule_is_a_deny(self):
        _policy, permissions_ = console([], answer=None)
        decision = permissions_.authorize("run_bash", {"command": "ls"})
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.verb, DENY)

    def test_deny_never_prompts(self):
        _policy, permissions_ = console([{"tool": "*", "arg": "*", "verb": DENY}],
                                        answer=None)
        self.assertFalse(permissions_.authorize(
            "run_bash", {"command": "rm -rf /"}).allowed)

    def test_wildcard_covers_unknown_tools(self):
        _policy, permissions_ = console([{"tool": "*", "arg": "*", "verb": ALLOW}],
                                        answer=None)
        self.assertTrue(permissions_.authorize("some_mcp__tool", {"a": 1}).allowed)


class AskTests(unittest.TestCase):
    def test_ask_becomes_deny_when_nobody_can_answer(self):
        _policy, permissions_ = console(ASK_ALL, interactive=False, answer=None)
        decision = permissions_.authorize("run_bash", {"command": "rm -rf /"})
        self.assertFalse(decision.allowed)
        self.assertIn("non-interactive", decision.note)

    def test_allow_once_does_not_touch_the_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "policy.json")
            permissions.write_policy(path, {"version": 1, "rules": ASK_ALL})
            _policy, permissions_ = console(ASK_ALL, path=path, answer="y")
            self.assertTrue(permissions_.authorize(
                "run_bash", {"command": "git status"}).allowed)
            self.assertEqual(load_json(path)["rules"], ASK_ALL)

    def test_always_allow_writes_a_scoped_rule_at_the_top(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "policy.json")
            permissions.write_policy(path, {"version": 1, "rules": ASK_ALL})
            policy, permissions_ = console(ASK_ALL, path=path, answer="a")

            decision = permissions_.authorize(
                "run_bash", {"command": "git status --short"})

            self.assertTrue(decision.allowed)
            expected = {"tool": "run_bash", "arg": "git *", "verb": ALLOW}
            self.assertEqual(policy["rules"][0], expected)
            self.assertEqual(load_json(path)["rules"][0], expected)

    def test_deny_answer_denies(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "policy.json")
            permissions.write_policy(path, {"version": 1, "rules": ASK_ALL})
            _policy, permissions_ = console(ASK_ALL, path=path, answer="d")
            self.assertFalse(permissions_.authorize(
                "run_bash", {"command": "ls"}).allowed)

    def test_always_allow_takes_effect_on_the_next_call(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "policy.json")
            permissions.write_policy(path, {"version": 1, "rules": ASK_ALL})
            policy, permissions_ = console(ASK_ALL, path=path, answer="a")
            permissions_.authorize("write_file",
                                   {"path": "src/main.py", "content": "x"})

            def refuse_prompt(*_args):
                raise AssertionError("the new rule should have matched first")

            second = Permissions(policy, path, interactive=True, prompt_fn=refuse_prompt)
            self.assertTrue(second.authorize(
                "write_file", {"path": "src/other.py", "content": "y"}).allowed)


class DerivationTests(unittest.TestCase):
    def test_bash_rule_widens_to_the_first_token(self):
        self.assertEqual(
            permissions.derive_rule("run_bash", {"command": "git status --short"}),
            {"tool": "run_bash", "arg": "git *", "verb": ALLOW})

    def test_bash_rule_refuses_to_widen_across_metacharacters(self):
        self.assertEqual(
            permissions.derive_rule("run_bash", {"command": "ls; rm -rf /"}),
            {"tool": "run_bash", "arg": "ls; rm -rf /", "verb": ALLOW})

    def test_single_word_bash_rule_stays_exact(self):
        self.assertEqual(permissions.derive_rule("run_bash", {"command": "ls"}),
                         {"tool": "run_bash", "arg": "ls", "verb": ALLOW})

    def test_file_rule_uses_the_extension(self):
        self.assertEqual(permissions.derive_rule("write_file", {"path": "src/main.py"}),
                         {"tool": "write_file", "arg": "src/*.py", "verb": ALLOW})

    def test_file_rule_without_extension_stays_exact(self):
        self.assertEqual(permissions.derive_rule("edit_file", {"path": "Makefile"}),
                         {"tool": "edit_file", "arg": "Makefile", "verb": ALLOW})

    def test_mcp_rule_is_tool_level(self):
        self.assertEqual(permissions.derive_rule("fake__echo", {"text": "hi"}),
                         {"tool": "fake__echo", "arg": "*", "verb": ALLOW})

    def test_trailing_wildcard_also_matches_the_bare_command(self):
        rule = {"tool": "run_bash", "arg": "git status *", "verb": ALLOW}
        self.assertTrue(permissions.rule_matches(rule, "run_bash", "git status"))
        self.assertTrue(permissions.rule_matches(rule, "run_bash", "git status --short"))
        self.assertFalse(permissions.rule_matches(rule, "run_bash", "git push"))


class LoadTests(unittest.TestCase):
    def test_malformed_policy_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "policy.json")
            with open(path, "w") as handle:
                handle.write("{ not json")
            with self.assertRaises(PolicyError):
                permissions.load_policy(path)

    def test_unknown_verb_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "policy.json")
            permissions.write_policy(path, {"version": 1, "rules": [
                {"tool": "*", "arg": "*", "verb": "maybe"}]})
            with self.assertRaises(PolicyError):
                permissions.load_policy(path)

    def test_ensure_policy_writes_the_default_once(self):
        with tempfile.TemporaryDirectory() as directory:
            path = permissions.ensure_policy(directory)
            rules = permissions.load_policy(path)["rules"]
            self.assertEqual(rules[-1], {"tool": "*", "arg": "*", "verb": ASK})
            permissions.write_policy(path, {"version": 1, "rules": []})
            permissions.ensure_policy(directory)
            self.assertEqual(permissions.load_policy(path)["rules"], [])

    def test_trust_policy_prompts_once_then_remembers(self):
        with tempfile.TemporaryDirectory() as directory:
            path = permissions.ensure_policy(directory)
            asks = []

            def prompt(_question):
                asks.append(1)
                return True

            permissions.trust_policy(directory, path, prompt_fn=prompt, out=lambda _: None)
            permissions.trust_policy(directory, path, prompt_fn=prompt, out=lambda _: None)
            self.assertEqual(len(asks), 1)

    def test_trust_policy_refuses_when_there_is_nobody_to_ask(self):
        with tempfile.TemporaryDirectory() as directory:
            path = permissions.ensure_policy(directory)
            with self.assertRaises(PolicyError):
                permissions.trust_policy(directory, path, interactive=False,
                                         out=lambda _: None)


class EndToEndTests(unittest.TestCase):
    def test_always_allow_persists_through_a_turn(self):
        with tempfile.TemporaryDirectory() as directory:
            root = os.path.realpath(directory)
            replies = [
                assistant(tool_calls=[call("write_file",
                                           {"path": "notes.txt", "content": "hi"})]),
                assistant("done"),
            ]
            harness, _model, _out = build_harness(
                root, replies, rules=ASK_ALL, interactive=True, answers=["a"])

            harness.run_user_turn("write notes")

            saved = load_json(harness.permissions.path)["rules"]
            self.assertIn({"tool": "write_file", "arg": "*.txt", "verb": ALLOW}, saved)
            self.assertTrue(os.path.exists(os.path.join(root, "notes.txt")))

    def test_ask_is_denied_inside_a_subagent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = os.path.realpath(directory)
            replies = [
                assistant(tool_calls=[call("task", {"prompt": "try to write"})]),
                assistant(tool_calls=[call("write_file",
                                           {"path": "nope.txt", "content": "x"})]),
                assistant("could not write"),
                assistant("done"),
            ]
            rules = [{"tool": "task", "arg": "*", "verb": ALLOW}, ASK_ALL[-1]]
            harness, _model, _out = build_harness(
                root, replies, rules=rules, interactive=True, answers=[])

            reply = harness.run_user_turn("delegate a write")

            self.assertEqual(reply, "done")
            self.assertFalse(os.path.exists(os.path.join(root, "nope.txt")))
            sub_path = os.path.join(harness.sessions_dir, harness.session.id + ".sub-1.jsonl")
            with open(sub_path) as handle:
                events = [json.loads(line) for line in handle]
            denials = [event for event in events
                       if event.get("t") == "tool" and event.get("decision") == ASK]
            self.assertEqual(len(denials), 1)
            self.assertIn("non-interactive", denials[0]["note"])


if __name__ == "__main__":
    unittest.main()
