"""Hooks: matching, blocking, capture, trust, and the loop invariants under them."""

import json
import os
import tempfile
import unittest

from macroharness import hooks as hooks_mod, permissions
from macroharness.tools import Containment
from tests.fake_model import assistant, build_harness, call


class ValidationTest(unittest.TestCase):
    def test_defaults_are_filled_in(self):
        config = hooks_mod.validate({"hooks": [{"event": "turn_end", "run": "true"}]})
        hook = config["hooks"][0]
        self.assertEqual((hook["tool"], hook["arg"]), ("*", "*"))
        self.assertFalse(hook["blocking"])
        self.assertFalse(hook["capture"])

    def test_rejects_an_unknown_event(self):
        with self.assertRaises(hooks_mod.HookError):
            hooks_mod.validate({"hooks": [{"event": "whenever", "run": "true"}]})

    def test_rejects_an_empty_run(self):
        with self.assertRaises(hooks_mod.HookError):
            hooks_mod.validate({"hooks": [{"event": "turn_end", "run": "  "}]})

    def test_rejects_a_missing_hooks_list(self):
        with self.assertRaises(hooks_mod.HookError):
            hooks_mod.validate({"version": 1})


class MatchingTest(unittest.TestCase):
    def hooks(self, entries):
        return hooks_mod.Hooks(None, config=hooks_mod.validate({"hooks": entries}))

    def test_matches_on_the_same_globs_as_the_policy(self):
        hooks = self.hooks([{"event": "pre_tool", "tool": "write_file",
                             "arg": "*.py", "run": "true"}])
        self.assertEqual(len(hooks.matching("pre_tool", "write_file", "a.py")), 1)
        self.assertEqual(hooks.matching("pre_tool", "write_file", "a.txt"), [])
        self.assertEqual(hooks.matching("pre_tool", "read_file", "a.py"), [])

    def test_lifecycle_events_ignore_tool_globs(self):
        hooks = self.hooks([{"event": "turn_end", "run": "true"}])
        self.assertEqual(len(hooks.matching("turn_end")), 1)

    def test_an_empty_hook_list_matches_nothing(self):
        self.assertEqual(self.hooks([]).matching("pre_tool", "write_file", "a"), [])


class LoopIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = os.path.realpath(self.directory.name)

    def test_blocking_pre_tool_hook_cancels_the_call(self):
        replies = [
            assistant(tool_calls=[call("write_file",
                                       {"path": "a.txt", "content": "hi"}, call_id="c1")]),
            assistant("understood"),
        ]
        harness, _model, _out = build_harness(
            self.root, replies,
            hooks=[{"event": "pre_tool", "tool": "write_file", "arg": "*",
                    "run": "echo 'writes are frozen' >&2; exit 1", "blocking": True}])

        harness.run_user_turn("write a file")

        # The call never ran, and the refusal is the tool result the model read.
        self.assertFalse(os.path.exists(os.path.join(self.root, "a.txt")))
        self.assertIn("blocked by hook", harness.messages[3]["content"])
        self.assertIn("writes are frozen", harness.messages[3]["content"])

    def test_a_blocked_call_still_gets_exactly_one_result_in_order(self):
        replies = [
            assistant(tool_calls=[
                call("read_file", {"path": "a.txt"}, call_id="c1"),
                call("write_file", {"path": "a.txt", "content": "x"}, call_id="c2"),
            ]),
            assistant("ok"),
        ]
        with open(os.path.join(self.root, "a.txt"), "w") as handle:
            handle.write("here")
        harness, _model, _out = build_harness(
            self.root, replies,
            hooks=[{"event": "pre_tool", "tool": "write_file", "arg": "*",
                    "run": "exit 1", "blocking": True}])

        harness.run_user_turn("read then write")

        results = [m for m in harness.messages if m["role"] == "tool"]
        self.assertEqual([r["tool_call_id"] for r in results], ["c1", "c2"])
        self.assertIn("here", results[0]["content"])
        self.assertIn("blocked by hook", results[1]["content"])

    def test_non_blocking_pre_tool_failure_lets_the_call_through(self):
        replies = [
            assistant(tool_calls=[call("write_file", {"path": "a.txt", "content": "hi"})]),
            assistant("ok"),
        ]
        harness, _model, _out = build_harness(
            self.root, replies,
            hooks=[{"event": "pre_tool", "tool": "*", "arg": "*", "run": "exit 3"}])

        harness.run_user_turn("write a file")

        self.assertTrue(os.path.exists(os.path.join(self.root, "a.txt")))

    def test_capturing_post_tool_hook_appends_to_the_tool_result(self):
        replies = [
            assistant(tool_calls=[call("write_file",
                                       {"path": "a.py", "content": "x="}, call_id="c1")]),
            assistant("ok"),
        ]
        harness, _model, _out = build_harness(
            self.root, replies,
            hooks=[{"event": "post_tool", "tool": "write_file", "arg": "*.py",
                    "run": "echo 'lint: syntax error'", "capture": True}])

        harness.run_user_turn("write a file")

        content = harness.messages[3]["content"]
        self.assertIn("wrote 2 bytes to a.py", content)
        self.assertIn("lint: syntax error", content)

    def test_post_tool_hook_without_capture_does_not_change_the_result(self):
        replies = [
            assistant(tool_calls=[call("write_file", {"path": "a.py", "content": "x"})]),
            assistant("ok"),
        ]
        harness, _model, _out = build_harness(
            self.root, replies,
            hooks=[{"event": "post_tool", "tool": "*", "arg": "*", "run": "echo noise"}])

        harness.run_user_turn("write a file")

        self.assertEqual(harness.messages[3]["content"], "wrote 1 bytes to a.py")

    def test_hooks_see_the_tool_and_argument_in_the_environment(self):
        replies = [
            assistant(tool_calls=[call("write_file", {"path": "a.py", "content": "x"})]),
            assistant("ok"),
        ]
        harness, _model, _out = build_harness(
            self.root, replies,
            hooks=[{"event": "post_tool", "tool": "*", "arg": "*",
                    "run": 'echo "saw $MH_TOOL $MH_ARG"', "capture": True}])

        harness.run_user_turn("write a file")

        self.assertIn("saw write_file a.py", harness.messages[3]["content"])

    def test_hooks_receive_the_call_as_json_on_stdin(self):
        replies = [
            assistant(tool_calls=[call("write_file", {"path": "a.py", "content": "x"})]),
            assistant("ok"),
        ]
        harness, _model, _out = build_harness(
            self.root, replies,
            hooks=[{"event": "post_tool", "tool": "*", "arg": "*",
                    "run": "cat", "capture": True}])

        harness.run_user_turn("write a file")

        captured = harness.messages[3]["content"]
        payload = json.loads(captured.split("]\n", 1)[1])
        self.assertEqual(payload["tool"], "write_file")
        self.assertEqual(payload["arguments"]["path"], "a.py")

    def test_turn_end_hook_fires_once_per_turn(self):
        marker = os.path.join(self.root, "turns")
        harness, _model, _out = build_harness(
            self.root, [assistant("one"), assistant("two")],
            hooks=[{"event": "turn_end", "run": "echo . >> turns"}])

        harness.run_user_turn("first")
        harness.run_user_turn("second")

        with open(marker) as handle:
            self.assertEqual(len(handle.read().split()), 2)

    def test_session_end_hook_fires_on_close(self):
        harness, _model, _out = build_harness(
            self.root, [assistant("done")],
            hooks=[{"event": "session_end", "run": "echo bye > farewell"}])
        harness.run_user_turn("hi")
        harness.close()
        self.assertTrue(os.path.exists(os.path.join(self.root, "farewell")))

    def test_hooks_run_inside_the_workspace(self):
        replies = [
            assistant(tool_calls=[call("write_file", {"path": "a.py", "content": "x"})]),
            assistant("ok"),
        ]
        harness, _model, _out = build_harness(
            self.root, replies,
            hooks=[{"event": "post_tool", "tool": "*", "arg": "*",
                    "run": "pwd", "capture": True}])
        harness.run_user_turn("write")
        self.assertIn(self.root, harness.messages[3]["content"])

    def test_a_hook_cannot_widen_a_denied_call(self):
        """Hooks run after the policy, so a deny is never reconsidered."""
        replies = [
            assistant(tool_calls=[call("write_file", {"path": "a.txt", "content": "x"})]),
            assistant("ok"),
        ]
        harness, _model, _out = build_harness(
            self.root, replies,
            rules=[{"tool": "write_file", "arg": "*", "verb": "deny"},
                   {"tool": "*", "arg": "*", "verb": "allow"}],
            hooks=[{"event": "pre_tool", "tool": "*", "arg": "*", "run": "exit 0"}])

        harness.run_user_turn("write a file")

        self.assertIn("denied", harness.messages[3]["content"])
        self.assertFalse(os.path.exists(os.path.join(self.root, "a.txt")))


class TrustTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = os.path.realpath(self.directory.name)
        self.state = os.path.join(self.root, ".macroharness")
        os.makedirs(self.state)
        self.containment = Containment(self.root)
        self.lines = []

    def write_hooks(self, entries):
        path = hooks_mod.hooks_path(self.state)
        hooks_mod.write_config(path, {"version": 1, "hooks": entries})
        return path

    def test_untrusted_hooks_do_not_run_when_nobody_can_be_asked(self):
        self.write_hooks([{"event": "turn_end", "run": "touch pwned"}])
        hooks = hooks_mod.Hooks.load(self.containment, self.state,
                                     interactive=False, out=self.lines.append)
        self.assertEqual(hooks.hooks, [])
        hooks.fire("turn_end")
        self.assertFalse(os.path.exists(os.path.join(self.root, "pwned")))

    def test_trusted_hooks_load_without_a_prompt(self):
        path = self.write_hooks([{"event": "turn_end", "run": "true"}])
        permissions.mark_trusted(self.state, path, key=hooks_mod.TRUST_KEY)
        hooks = hooks_mod.Hooks.load(self.containment, self.state,
                                     interactive=False, out=self.lines.append)
        self.assertEqual(len(hooks.hooks), 1)

    def test_editing_the_file_revokes_trust(self):
        path = self.write_hooks([{"event": "turn_end", "run": "true"}])
        permissions.mark_trusted(self.state, path, key=hooks_mod.TRUST_KEY)
        hooks = hooks_mod.Hooks.load(self.containment, self.state,
                                     interactive=False, out=self.lines.append)
        self.assertEqual(len(hooks.hooks), 1)

        self.write_hooks([{"event": "turn_end", "run": "touch pwned"}])
        hooks.reload()

        self.assertEqual(hooks.hooks, [])

    def test_an_empty_hook_file_needs_no_trust(self):
        self.write_hooks([])
        hooks = hooks_mod.Hooks.load(self.containment, self.state,
                                     interactive=False, out=self.lines.append)
        self.assertEqual(hooks.hooks, [])
        self.assertEqual(self.lines, [])

    def test_a_malformed_hook_file_is_ignored_not_fatal(self):
        with open(hooks_mod.hooks_path(self.state), "w") as handle:
            handle.write("{not json")
        hooks = hooks_mod.Hooks.load(self.containment, self.state,
                                     interactive=False, out=self.lines.append)
        self.assertEqual(hooks.hooks, [])
        self.assertTrue(any("ignoring" in line for line in self.lines))


class DefineHookTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = os.path.realpath(self.directory.name)
        self.state = os.path.join(self.root, ".macroharness")
        os.makedirs(self.state)
        self.hooks = hooks_mod.Hooks.load(Containment(self.root), self.state,
                                          out=lambda _line: None)
        self.define = hooks_mod.define_hook_tool(self.hooks).func

    def test_defining_a_hook_takes_effect_immediately(self):
        result = self.define(event="turn_end", run="echo hi")
        self.assertIn("added hook", result)
        self.assertEqual(len(self.hooks.hooks), 1)

    def test_a_defined_hook_is_written_and_trusted(self):
        self.define(event="turn_end", run="echo hi")
        with open(hooks_mod.hooks_path(self.state)) as handle:
            self.assertEqual(len(json.load(handle)["hooks"]), 1)
        self.assertIn(hooks_mod.TRUST_KEY, permissions.trusted_digests(self.state))

    def test_a_defined_hook_survives_a_reload_without_reprompting(self):
        self.define(event="turn_end", run="echo hi")
        self.hooks._digest = None  # force the file to be re-read
        self.hooks.reload()
        self.assertEqual(len(self.hooks.hooks), 1)

    def test_an_invalid_hook_is_an_error_not_an_exception(self):
        self.assertTrue(self.define(event="nope", run="x").startswith("error:"))
        self.assertEqual(self.hooks.hooks, [])

    def test_defining_appends_rather_than_replacing(self):
        self.define(event="turn_end", run="first")
        self.define(event="session_end", run="second")
        self.assertEqual([h["run"] for h in self.hooks.hooks], ["first", "second"])


if __name__ == "__main__":
    unittest.main()
