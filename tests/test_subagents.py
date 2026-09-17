"""Subagents: context isolation, shared budget, and no nested delegation."""

import os
import tempfile
import unittest

from tests.fake_model import assistant, build_harness, call


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


if __name__ == "__main__":
    unittest.main()
