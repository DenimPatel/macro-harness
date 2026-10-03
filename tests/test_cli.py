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

    def test_build_loads_agent_profiles_and_offers_them_to_task(self):
        agents = os.path.join(self.root, ".macroharness", "agents")
        os.makedirs(agents)
        with open(os.path.join(agents, "verifier.md"), "w") as handle:
            handle.write("---\ndescription: checks work\nmax_steps: 5\n---\nBe skeptical.")
        harness = self.build()
        self.addCleanup(harness.close)
        self.assertEqual(sorted(harness.agents), ["verifier"])
        self.assertIn("verifier", harness.registry.get("task").parameters["properties"]["agent"]["enum"])

    def test_a_malformed_agent_profile_is_a_clear_error(self):
        agents = os.path.join(self.root, ".macroharness", "agents")
        os.makedirs(agents)
        with open(os.path.join(agents, "broken.md"), "w") as handle:
            handle.write("---\nmax_steps: many\n---\n")
        with self.assertRaises(PolicyError) as caught:
            self.build()
        self.assertIn("broken", str(caught.exception))

    def test_bash_timeout_and_subagent_steps_are_wired(self):
        harness = self.build("--bash-timeout", "1", "--subagent-max-steps", "40")
        self.addCleanup(harness.close)
        self.assertEqual(harness.subagent_max_steps, 40)
        result = harness.registry.get("run_bash").func(command="sleep 3")
        self.assertIn("timed out after 1s", result)

    def test_resume_without_an_id_takes_the_latest(self):
        first = self.build()
        first.run_user_turn("first session")
        first.close()
        resumed = self.build("--resume")
        self.addCleanup(resumed.close)
        self.assertEqual(resumed.session.id, first.session.id)


class OneShotTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = os.path.realpath(self.directory.name)

    def test_prompt_file_is_read_and_joined_with_trailing_words(self):
        path = os.path.join(self.root, "ORCHESTRATOR.md")
        with open(path, "w") as handle:
            handle.write("# Orchestrator\nBegin with Phase 1.\n")
        harness, model, _out = build_harness(self.root, [assistant("started")])
        with mock.patch.object(cli, "build", return_value=harness):
            self.assertEqual(cli.main(["--prompt-file", path, "FOCUS:", "docs"]), 0)
        self.assertEqual(model.requests[0]["messages"][-1]["content"],
                         "# Orchestrator\nBegin with Phase 1.\n\nFOCUS: docs")

    def test_a_missing_prompt_file_exits_with_a_message(self):
        with self.assertRaises(SystemExit) as caught:
            cli.main(["--prompt-file", os.path.join(self.root, "nope.md")])
        self.assertIn("--prompt-file", str(caught.exception.code))

    def test_rounds_continue_the_same_session(self):
        replies = [assistant("did item 1"), assistant("did item 2"), assistant("did item 3")]
        harness, model, out = build_harness(self.root, replies)
        self.assertEqual(cli.run_rounds(harness, "improve the repo", rounds=3), 3)
        users = [m["content"] for m in model.requests[2]["messages"] if m["role"] == "user"]
        self.assertEqual(users[0], "improve the repo")
        self.assertEqual(users[1:], [cli.CONTINUE_PROMPT] * 2)
        self.assertIn("[round 3/3]", out.text)

    def test_a_stop_file_ends_the_rounds_early(self):
        def stop_after_first(_messages, _tools, on_text=None):
            os.makedirs(os.path.join(self.root, ".improve"), exist_ok=True)
            open(os.path.join(self.root, ".improve", "STOP"), "w").close()
            return original(_messages, _tools, on_text)

        harness, model, out = build_harness(self.root, [assistant("one"), assistant("two")])
        original = model.complete
        model.complete = stop_after_first
        done = cli.run_rounds(harness, "go", rounds=5, stop_file=".improve/STOP")
        self.assertEqual(done, 1)
        self.assertIn("stop file .improve/STOP exists", out.text)

    def test_rounds_stop_at_the_budget(self):
        replies = [assistant("a", usage={"prompt_tokens": 90, "completion_tokens": 20}),
                   assistant("b")]
        harness, _model, _out = build_harness(self.root, replies, budget=100)
        self.assertEqual(cli.run_rounds(harness, "go", rounds=4), 1)


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

    def test_mcp_and_agents_commands_describe_what_is_loaded(self):
        import sys as _sys
        from macroharness.subagents import parse_agent
        server = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake_mcp_server.py")
        agents = {"verifier": parse_agent("verifier", "---\ndescription: checks\n---\nx")}
        harness, _model, out = build_harness(
            self.root, [], agents=agents,
            mcp_config={"servers": {"fake": {"command": _sys.executable, "args": [server]}}})
        self.addCleanup(harness.close)
        cli.meta(harness, "/mcp", out)
        cli.meta(harness, "/agents", out)
        self.assertIn("fake: 1 tool(s), timeout 20s", out.text)
        self.assertIn("fake__echo", out.text)
        self.assertIn("verifier", out.text)
        self.assertIn("tools: all; max_steps: default", out.text)

    def test_mcp_and_agents_commands_say_when_nothing_is_loaded(self):
        harness, _model, out = build_harness(self.root, [])
        cli.meta(harness, "/mcp", out)
        cli.meta(harness, "/agents", out)
        self.assertIn("no MCP servers", out.text)
        self.assertIn("no agent profiles", out.text)

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
