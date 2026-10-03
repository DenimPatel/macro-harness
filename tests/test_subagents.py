"""Subagents: context isolation, shared budget, and no nested delegation."""

import os
import sys
import tempfile
import threading
import unittest

from macroharness.subagents import AgentError, load_agents, parse_agent
from tests.fake_model import RoutingModel, assistant, build_harness, call

FAKE_SERVER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake_mcp_server.py")

VERIFIER = """---
description: independent skeptical reviewer
tools: read_file, run_bash, fake__*
max_steps: 3
---
You did not write this change. Report VERDICT: PASS or FAIL.
"""


class SubagentTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = os.path.realpath(self.directory.name)

    def test_task_runs_a_nested_loop_and_returns_only_its_answer(self):
        replies = [
            assistant(tool_calls=[call("task", {"prompt": "count the files"})]),
            assistant("there are 3 files"),
            assistant("done"),
        ]
        harness, model, _out = build_harness(self.root, replies)

        reply = harness.run_user_turn("delegate the counting")

        self.assertEqual(reply, "done")
        self.assertEqual([m["role"] for m in harness.messages],
                         ["system", "user", "assistant", "tool", "assistant"])
        self.assertIn("there are 3 files", harness.messages[3]["content"])
        self.assertIn("subagent:", harness.messages[3]["content"])
        # Three model calls: the parent's, the subagent's, and the parent's again.
        self.assertEqual(len(model.requests), 3)
        # The nested transcript never leaks: the parent's last request holds only
        # its own four messages, and none of the subagent's system prompt.
        final_request = model.requests[2]["messages"]
        self.assertEqual(len(final_request), 4)
        self.assertFalse(any("You are a subagent" in (message.get("content") or "")
                             for message in final_request))

    def test_the_subagent_gets_its_own_session_log(self):
        replies = [
            assistant(tool_calls=[call("task", {"prompt": "look around"})]),
            assistant("nothing to report"),
            assistant("done"),
        ]
        harness, _model, _out = build_harness(self.root, replies)
        harness.run_user_turn("delegate")

        path = os.path.join(harness.sessions_dir, harness.session.id + ".sub-1.jsonl")
        self.assertTrue(os.path.exists(path))
        with open(path) as handle:
            kinds = [line.split('"t": "')[1].split('"')[0] for line in handle]
        self.assertEqual(kinds[0], "system")
        self.assertIn("user", kinds)
        self.assertIn("assistant", kinds)

    def test_depth_two_is_refused(self):
        harness, _model, _out = build_harness(self.root, [])
        harness.depth = 1
        self.assertIn("cannot spawn subagents", harness.spawn_subagent("nested"))

    def test_subagent_tokens_count_against_the_parent(self):
        replies = [
            assistant(tool_calls=[call("task", {"prompt": "look around"})],
                      usage={"prompt_tokens": 20, "completion_tokens": 5}),
            assistant("nothing to report",
                      usage={"prompt_tokens": 30, "completion_tokens": 10}),
            assistant("done", usage={"prompt_tokens": 50, "completion_tokens": 5}),
        ]
        harness, _model, _out = build_harness(self.root, replies)
        harness.run_user_turn("delegate")
        self.assertEqual(harness.accounting.prompt_tokens, 100)
        self.assertEqual(harness.accounting.completion_tokens, 20)
        self.assertEqual(harness.accounting.calls, 3)

    def test_subagent_that_runs_out_of_steps_says_so(self):
        replies = [
            assistant(tool_calls=[call("task", {"prompt": "loop"})]),
            assistant(tool_calls=[call("run_bash", {"command": "true"})]),
        ]
        harness, _model, _out = build_harness(self.root, replies, max_steps=1)

        harness.run_user_turn("delegate")

        self.assertIn("reached 1 tool rounds", harness.messages[3]["content"])



class AgentProfileTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = os.path.realpath(self.directory.name)

    def write_agent(self, name, text):
        directory = os.path.join(self.root, ".macroharness", "agents")
        os.makedirs(directory, exist_ok=True)
        with open(os.path.join(directory, name + ".md"), "w") as handle:
            handle.write(text)

    def test_parse_reads_the_header_and_keeps_the_body(self):
        agent = parse_agent("verifier", VERIFIER)
        self.assertEqual(agent.description, "independent skeptical reviewer")
        self.assertEqual(agent.tools, ["read_file", "run_bash", "fake__*"])
        self.assertEqual(agent.max_steps, 3)
        self.assertTrue(agent.prompt.startswith("You did not write this change."))

    def test_a_file_without_a_header_is_all_prompt(self):
        agent = parse_agent("plain", "Just do the thing.")
        self.assertIsNone(agent.tools)
        self.assertIsNone(agent.max_steps)
        self.assertEqual(agent.prompt, "Just do the thing.")

    def test_malformed_headers_are_refused(self):
        for text in ("---\ndescription: x\n", "---\ncolour: red\n---\n",
                     "---\nmax_steps: lots\n---\n", "---\nmax_steps: 0\n---\n"):
            with self.assertRaises(AgentError):
                parse_agent("bad", text)

    def test_load_agents_reads_the_directory(self):
        self.write_agent("verifier", VERIFIER)
        self.write_agent("implementer", "Write the code.")
        agents = load_agents(os.path.join(self.root, ".macroharness"))
        self.assertEqual(sorted(agents), ["implementer", "verifier"])
        self.assertEqual(load_agents(os.path.join(self.root, "missing")), {})

    def test_the_task_schema_offers_the_agent_names(self):
        agents = {"verifier": parse_agent("verifier", VERIFIER)}
        harness, _model, _out = build_harness(self.root, [], agents=agents)
        schema = harness.registry.get("task").parameters
        self.assertEqual(schema["properties"]["agent"]["enum"], ["verifier"])
        self.assertIn("independent skeptical reviewer",
                      harness.registry.get("task").description)

    def test_a_named_agent_gets_its_prompt_its_tools_and_its_step_limit(self):
        agents = {"verifier": parse_agent("verifier", VERIFIER)}
        replies = [
            assistant(tool_calls=[call("task", {"prompt": "check it", "agent": "verifier"})]),
            # The verifier tries to write: write_file is not in its toolset.
            assistant(tool_calls=[call("write_file", {"path": "x", "content": "y"})]),
            assistant(tool_calls=[call("run_bash", {"command": "true"})]),
            assistant(tool_calls=[call("run_bash", {"command": "true"})]),
            assistant("parent done"),
        ]
        harness, model, out = build_harness(self.root, replies, agents=agents, max_steps=8)

        harness.run_user_turn("verify")

        sub_request = model.requests[1]
        system = sub_request["messages"][0]["content"]
        self.assertIn("Role: verifier", system)
        self.assertIn("VERDICT", system)
        self.assertIn("Your tools are: read_file, run_bash.", system)
        names = sorted(tool["function"]["name"] for tool in sub_request["tools"])
        self.assertEqual(names, ["read_file", "run_bash"])
        self.assertFalse(os.path.exists(os.path.join(self.root, "x")))
        # max_steps: 3 from the profile, not the parent's 8.
        self.assertIn("reached 3 tool rounds", harness.messages[3]["content"])
        self.assertIn("[sub-1:verifier]", out.text)
        self.assertIn("unknown tool", out.text)

    def test_an_unknown_agent_is_a_tool_result(self):
        replies = [
            assistant(tool_calls=[call("task", {"prompt": "x", "agent": "ghost"})]),
            assistant("ok"),
        ]
        agents = {"verifier": parse_agent("verifier", VERIFIER)}
        harness, _model, _out = build_harness(self.root, replies, agents=agents)
        harness.run_user_turn("go")
        self.assertIn("unknown agent 'ghost'", harness.messages[3]["content"])
        self.assertIn("verifier", harness.messages[3]["content"])

    def test_subagent_max_steps_raises_the_default_cap(self):
        replies = [assistant(tool_calls=[call("task", {"prompt": "work"})])]
        replies += [assistant(tool_calls=[call("run_bash", {"command": "true"})])] * 12
        replies += [assistant("sub done"), assistant("parent done")]
        harness, _model, _out = build_harness(
            self.root, replies, max_steps=20, subagent_max_steps=20)
        harness.run_user_turn("go")
        self.assertIn("sub done", harness.messages[3]["content"])
        self.assertIn("13 steps", harness.messages[3]["content"])

    def test_subagents_can_use_mcp_tools(self):
        replies = [
            assistant(tool_calls=[call("task", {"prompt": "echo via mcp"})]),
            assistant(tool_calls=[call("fake__echo", {"text": "from a subagent"})]),
            assistant("the server said hi"),
            assistant("done"),
        ]
        harness, model, _out = build_harness(
            self.root, replies,
            mcp_config={"servers": {"fake": {"command": sys.executable,
                                             "args": [FAKE_SERVER]}}})
        self.addCleanup(harness.close)
        harness.run_user_turn("delegate")
        self.assertIn("fake__echo", model.requests[1]["messages"][0]["content"])
        sub_log = os.path.join(harness.sessions_dir, harness.session.id + ".sub-1.jsonl")
        with open(sub_log) as handle:
            self.assertIn("echo: from a subagent", handle.read())


class ParallelSubagentTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = os.path.realpath(self.directory.name)

    def test_task_calls_in_one_round_run_concurrently_and_land_in_order(self):
        # Every subagent's first model call waits at a barrier for the other
        # two. Run serially, the barrier would time out and break.
        barrier = threading.Barrier(3, timeout=10)

        def on_call(key, messages):
            if key.startswith("recon") and len(messages) == 2:
                barrier.wait()

        model = RoutingModel({
            "orchestrate": [
                assistant(tool_calls=[
                    call("task", {"prompt": "recon-a: classify"}, "c1"),
                    call("task", {"prompt": "recon-b: runbook"}, "c2"),
                    call("task", {"prompt": "recon-c: architect"}, "c3"),
                ]),
                assistant("profile written"),
            ],
            "recon-a": [assistant("A: a python library")],
            "recon-b": [assistant("B: unittest works")],
            "recon-c": [assistant("C: nine modules")],
        }, on_call=on_call)
        harness, _model, _out = build_harness(self.root, [], model=model)

        reply = harness.run_user_turn("orchestrate the recon")

        self.assertEqual(reply, "profile written")
        self.assertFalse(barrier.broken)
        results = [m for m in harness.messages if m["role"] == "tool"]
        self.assertEqual([m["tool_call_id"] for m in results], ["c1", "c2", "c3"])
        self.assertTrue(results[0]["content"].startswith("A:"))
        self.assertTrue(results[1]["content"].startswith("B:"))
        self.assertTrue(results[2]["content"].startswith("C:"))
        self.assertEqual(harness.sub_count, 3)
        logs = sorted(name for name in os.listdir(harness.sessions_dir) if ".sub-" in name)
        self.assertEqual(len(logs), 3)
        self.assertEqual(harness.accounting.calls, 5)


if __name__ == "__main__":
    unittest.main()
