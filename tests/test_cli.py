"""The CLI surface: wiring a workspace, the API key check, and the REPL."""

import os
import tempfile
import unittest
from unittest import mock

from macroharness import __main__ as cli
from macroharness.permissions import PolicyError
from tests.fake_model import Collector, ScriptedModel, assistant, build_harness


class BuildTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = os.path.realpath(self.directory.name)

    def args(self, *extra):
        return cli.build_parser().parse_args(["--workspace", self.root] + list(extra))

    def build(self, *extra, **kwargs):
        return cli.build(self.args(*extra), model=ScriptedModel([assistant("hi")]),
                         trust_prompt=lambda _question: True, out=Collector(), **kwargs)

    def test_build_wires_the_workspace_state(self):
        harness = self.build()
        self.addCleanup(harness.close)
        state = os.path.join(self.root, ".macroharness")
        self.assertEqual(harness.root, self.root)
        self.assertTrue(os.path.exists(os.path.join(state, "policy.json")))
        self.assertTrue(os.path.exists(os.path.join(state, "mcp.json")))
        self.assertTrue(os.path.exists(os.path.join(state, ".gitignore")))
        self.assertTrue(os.path.isdir(os.path.join(state, "sessions")))
        self.assertIsNotNone(harness.registry.get("task"))
        self.assertIsNotNone(harness.registry.get("edit_file"))

    def test_build_runs_a_turn_with_the_injected_model(self):
        harness = self.build()
        self.addCleanup(harness.close)
        self.assertEqual(harness.run_user_turn("hi"), "hi")

    def test_main_runs_a_one_shot_prompt_without_the_repl(self):
        harness, model, out = build_harness(self.root, [assistant("the repo is small")])
        with mock.patch.object(cli, "build", return_value=harness):
            self.assertEqual(cli.main(["summarize", "this", "repo"]), 0)
        self.assertEqual(model.requests[0]["messages"][-1],
                         {"role": "user", "content": "summarize this repo"})
        self.assertIn("the repo is small", out.text)

    def test_missing_api_key_is_a_clear_error(self):
        os.environ.pop("MACRO_MISSING_KEY", None)
        with self.assertRaises(PolicyError) as caught:
            cli.build(self.args("--api-key-env", "MACRO_MISSING_KEY"),
                      trust_prompt=lambda _question: True, out=Collector())
        self.assertIn("MACRO_MISSING_KEY", str(caught.exception))

    def test_non_interactive_refuses_an_untrusted_policy(self):
        with self.assertRaises(PolicyError) as caught:
            cli.build(self.args("--non-interactive"),
                      model=ScriptedModel([assistant("hi")]), out=Collector())
        self.assertIn("not trusted", str(caught.exception))

    def test_resume_of_an_unknown_session_is_an_error(self):
        with self.assertRaises(PolicyError):
            self.build("--resume", "no-such-session")

    def test_resume_of_a_written_session_replays_it(self):
        first = self.build()
        first.run_user_turn("remember this")
        session_id = first.session.id
        first.close()

        resumed = self.build("--resume", session_id)
        self.addCleanup(resumed.close)
        self.assertEqual(resumed.session.id, session_id)
        self.assertIn("remember this",
                      [message.get("content") for message in resumed.messages])

    def test_resume_without_an_id_takes_the_latest(self):
        first = self.build()
        first.run_user_turn("first session")
        first.close()
        resumed = self.build("--resume")
        self.addCleanup(resumed.close)
        self.assertEqual(resumed.session.id, first.session.id)


class MetaTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = os.path.realpath(self.directory.name)

    def test_meta_commands_print_something_useful(self):
        harness, _model, out = build_harness(self.root, [assistant("hi")])
        harness.run_user_turn("hi")
        for command in ("/tokens", "/session", "/rules", "/compact"):
            cli.meta(harness, command, out)
        self.assertIn("total:", out.text)
        self.assertIn(harness.session.id, out.text)
        self.assertIn("policy:", out.text)
        self.assertIn("compact:", out.text)

    def test_unknown_command_says_so(self):
        harness, _model, out = build_harness(self.root, [assistant("hi")])
        self.assertFalse(cli.meta(harness, "/nonsense", out))
        self.assertIn("unknown command", out.text)

    def test_exit_asks_the_repl_to_stop(self):
        harness, _model, out = build_harness(self.root, [assistant("hi")])
        self.assertTrue(cli.meta(harness, "/exit", out))

    def test_repl_runs_a_turn_then_exits(self):
        harness, _model, out = build_harness(self.root, [assistant("hello")])
        with mock.patch("builtins.input", side_effect=["hi", "/exit"]):
            cli.repl(harness, out=out)
        self.assertIn("hello", out.text)
        self.assertIn("/tokens", out.text)


if __name__ == "__main__":
    unittest.main()
