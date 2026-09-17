"""The loop's job: append verbatim, pair every call with one result, keep going."""

import os
import tempfile
import time
import unittest

from macroharness import tools
from tests.fake_model import assistant, build_harness, call


class LoopTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = os.path.realpath(self.directory.name)

    def test_assistant_message_is_appended_verbatim_and_paired(self):
        replies = [
            assistant(tool_calls=[call("write_file",
                                       {"path": "a.txt", "content": "hi"}, call_id="c1")]),
            assistant("done"),
        ]
        harness, _model, _out = build_harness(self.root, replies)

        reply = harness.run_user_turn("write a file")

        self.assertEqual(reply, "done")
        self.assertEqual([m["role"] for m in harness.messages],
                         ["system", "user", "assistant", "tool", "assistant"])
        self.assertEqual(harness.messages[2]["tool_calls"][0]["id"], "c1")
        self.assertEqual(harness.messages[3]["tool_call_id"], "c1")
        self.assertEqual(harness.messages[3]["content"], "wrote 2 bytes to a.txt")
        self.assertTrue(os.path.exists(os.path.join(self.root, "a.txt")))

    def test_tool_failure_is_returned_as_text_and_the_loop_continues(self):
        replies = [
            assistant(tool_calls=[call("read_file", {"path": "missing.txt"})]),
            assistant("recovered"),
        ]
        harness, _model, _out = build_harness(self.root, replies)

        harness.run_user_turn("read a missing file")

        self.assertIn("error", harness.messages[3]["content"])
        self.assertEqual(harness.last_reply, "recovered")

    def test_unknown_tool_is_a_tool_result(self):
        replies = [
            assistant(tool_calls=[call("no_such_tool", {})]),
            assistant("ok"),
        ]
        harness, _model, _out = build_harness(self.root, replies)
        harness.run_user_turn("call nonsense")
        self.assertIn("unknown tool", harness.messages[3]["content"])

    def test_malformed_arguments_are_a_tool_result(self):
        replies = [
            assistant(tool_calls=[call("read_file", "{not json")]),
            assistant("ok"),
        ]
        harness, _model, _out = build_harness(self.root, replies)
        harness.run_user_turn("call with garbage")
        self.assertIn("invalid JSON", harness.messages[3]["content"])

    def test_bad_argument_names_are_a_tool_result(self):
        replies = [
            assistant(tool_calls=[call("read_file", {"nope": 1})]),
            assistant("ok"),
        ]
        harness, _model, _out = build_harness(self.root, replies)
        harness.run_user_turn("call with wrong kwargs")
        self.assertIn("bad arguments", harness.messages[3]["content"])

    def test_stops_at_max_steps(self):
        replies = [
            assistant(tool_calls=[call("read_file", {"path": "x.txt"})]),
            assistant(tool_calls=[call("read_file", {"path": "x.txt"})]),
            assistant("never reached"),
        ]
        harness, _model, _out = build_harness(self.root, replies, max_steps=2)
        reply = harness.run_user_turn("loop forever")
        self.assertIn("reached 2 tool rounds", reply)
        self.assertEqual(len(harness.model.replies), 1)

    def test_turn_without_tool_calls_ends_immediately(self):
        harness, _model, _out = build_harness(self.root, [assistant("hello")])
        self.assertEqual(harness.run_user_turn("hi"), "hello")
        self.assertEqual([m["role"] for m in harness.messages],
                         ["system", "user", "assistant"])

    def test_session_log_replays_to_the_same_messages(self):
        replies = [
            assistant(tool_calls=[call("write_file",
                                       {"path": "a.txt", "content": "hi"})]),
            assistant("done"),
        ]
        harness, _model, _out = build_harness(self.root, replies)
        harness.run_user_turn("write a file")
        self.assertEqual(harness.session.messages(), harness.messages)

    def test_denied_call_is_not_executed(self):
        replies = [
            assistant(tool_calls=[call("write_file",
                                       {"path": "a.txt", "content": "hi"})]),
            assistant("ok, I will not"),
        ]
        harness, _model, _out = build_harness(
            self.root, replies,
            rules=[{"tool": "write_file", "arg": "*", "verb": "deny"},
                   {"tool": "*", "arg": "*", "verb": "allow"}])
        harness.run_user_turn("write a file")
        self.assertFalse(os.path.exists(os.path.join(self.root, "a.txt")))
        self.assertIn("denied", harness.messages[3]["content"])


class ParallelToolTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = os.path.realpath(self.directory.name)

    @staticmethod
    def sleeper(name, seconds):
        def func():
            time.sleep(seconds)
            return name
        return tools.Tool(name, "sleep", {"type": "object", "properties": {}},
                          func, read_only=True)

    def test_results_land_in_call_order_even_when_completion_order_differs(self):
        # `slow` is issued first but finishes last. Sequential ordering would
        # append fast, slow; the invariant requires slow, fast.
        extras = [self.sleeper("slow", 0.3), self.sleeper("fast", 0.3)]
        replies = [
            assistant(tool_calls=[call("slow", {}, call_id="c1"),
                                  call("fast", {}, call_id="c2")]),
            assistant("done"),
        ]
        harness, _model, _out = build_harness(self.root, replies, extra_tools=extras)

        started = time.monotonic()
        harness.run_user_turn("run both")
        elapsed = time.monotonic() - started

        results = [m for m in harness.messages if m["role"] == "tool"]
        self.assertEqual([m["tool_call_id"] for m in results], ["c1", "c2"])
        self.assertEqual([m["content"] for m in results], ["slow", "fast"])
        self.assertLess(elapsed, 0.55)  # ~0.3s in parallel, ~0.6s one at a time

    def test_mutating_tools_are_not_run_in_parallel(self):
        # A write that is not read-only must not be joined by anything else, so
        # the elapsed time stays sequential.
        extras = [self.sleeper("read_a", 0.3)]
        write = tools.Tool("slow_write", "sleep",
                           {"type": "object", "properties": {}},
                           lambda: time.sleep(0.3), read_only=False)
        replies = [
            assistant(tool_calls=[call("read_a", {}, call_id="c1"),
                                  call("slow_write", {}, call_id="c2")]),
            assistant("done"),
        ]
        harness, _model, _out = build_harness(self.root, replies,
                                              extra_tools=extras + [write])
        started = time.monotonic()
        harness.run_user_turn("run both")
        self.assertGreaterEqual(time.monotonic() - started, 0.55)


if __name__ == "__main__":
    unittest.main()
