"""MCP: discovery, namespacing, and the permission pipeline applying unchanged."""

import os
import sys
import tempfile
import unittest

from tests.fake_model import assistant, build_harness, call

FAKE_SERVER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake_mcp_server.py")


def fake_config():
    return {"servers": {"fake": {"command": sys.executable, "args": [FAKE_SERVER]}}}


class McpTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = os.path.realpath(self.directory.name)

    def test_discovered_tools_are_namespaced_and_flagged_read_only(self):
        harness, _model, out = build_harness(
            self.root, [assistant("hi")], mcp_config=fake_config())
        self.addCleanup(harness.close)

        tool = harness.registry.get("fake__echo")
        self.assertIsNotNone(tool)
        self.assertTrue(tool.read_only)
        self.assertEqual([name for name in harness.registry.names() if name.startswith("fake__")],
                         ["fake__echo"])
        self.assertIn("fake -> 1 tool(s)", out.text)

    def test_a_discovered_tool_is_called_through_the_policy(self):
        replies = [
            assistant(tool_calls=[call("fake__echo", {"text": "hi"})]),
            assistant("done"),
        ]
        rules = [{"tool": "fake__echo", "arg": "*", "verb": "allow"}]
        harness, _model, _out = build_harness(
            self.root, replies, rules=rules, mcp_config=fake_config())
        self.addCleanup(harness.close)

        harness.run_user_turn("echo something")

        self.assertEqual(harness.messages[3]["content"], "echo: hi")

    def test_a_discovered_tool_is_denied_when_the_policy_says_so(self):
        replies = [
            assistant(tool_calls=[call("fake__echo", {"text": "hi"})]),
            assistant("fine"),
        ]
        rules = [{"tool": "fake__echo", "arg": "*", "verb": "deny"},
                 {"tool": "*", "arg": "*", "verb": "allow"}]
        harness, _model, _out = build_harness(
            self.root, replies, rules=rules, mcp_config=fake_config())
        self.addCleanup(harness.close)

        harness.run_user_turn("echo something")

        self.assertIn("denied", harness.messages[3]["content"])

    def test_a_broken_server_does_not_stop_the_harness(self):
        config = {"servers": {"broken": {"command": "macro-harness-not-a-real-binary"}}}
        harness, _model, out = build_harness(
            self.root, [assistant("still here")], mcp_config=config)

        self.assertIn("unavailable", out.text)
        self.assertEqual(harness.run_user_turn("hi"), "still here")


if __name__ == "__main__":
    unittest.main()
