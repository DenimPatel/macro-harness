"""MCP: discovery, namespacing, and the permission pipeline applying unchanged."""

import os
import sys
import tempfile
import unittest

from macroharness.mcp import McpError, McpServer
from tests.fake_model import assistant, build_harness, call

FAKE_SERVER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake_mcp_server.py")


def fake_config():
    return {"servers": {"fake": {"command": sys.executable, "args": [FAKE_SERVER]}}}


def browserish(**extra):
    spec = {"command": sys.executable, "args": [FAKE_SERVER, "--browserish"]}
    spec.update(extra)
    return spec


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



class BrowserServerTests(unittest.TestCase):
    """What a server like Playwright MCP does that an echo server does not."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = os.path.realpath(self.directory.name)

    def start(self, **extra):
        server = McpServer("web", browserish(**extra), self.root)
        self.addCleanup(server.close)
        return server, server.start()

    def test_requests_from_the_server_and_a_stderr_flood_do_not_stall_calls(self):
        # Each reply is preceded by a roots/list request reusing our id, and
        # ~96KB of stderr: more than a pipe holds if nobody drains it.
        server, tools = self.start(timeout=10)
        for _ in range(3):
            self.assertEqual(server.call("echo", {"text": "hi"}), "echo: hi")
        self.assertEqual(len(tools), 3)
        self.assertIn("noise", server.stderr_tail())

    def test_image_blocks_are_saved_to_disk_not_inlined(self):
        server, _tools = self.start()
        result = server.call("screenshot", {})
        self.assertIn("took a screenshot", result)
        self.assertIn("[image/png saved to .macroharness/mcp-media/web/", result)
        self.assertNotIn("iVBOR", result)  # no base64 in the transcript
        relative = result.split("saved to ")[1].split(" (")[0]
        with open(os.path.join(self.root, relative), "rb") as handle:
            self.assertEqual(handle.read(8), b"\x89PNG\r\n\x1a\n")

    def test_the_per_server_timeout_comes_from_the_config(self):
        server, _tools = self.start(timeout=1)
        self.assertEqual(server.timeout, 1)
        with self.assertRaises(McpError) as caught:
            server.call("slow", {"seconds": 3})
        self.assertIn("timed out after 1s", str(caught.exception))

    def test_schema_keys_providers_reject_are_stripped(self):
        harness, _model, _out = build_harness(
            self.root, [assistant("hi")], mcp_config={"servers": {"web": browserish()}})
        self.addCleanup(harness.close)
        parameters = harness.registry.get("web__screenshot").parameters
        self.assertNotIn("$schema", parameters)
        self.assertEqual(parameters["type"], "object")

    def test_a_screenshot_through_the_loop_costs_one_line_of_context(self):
        replies = [assistant(tool_calls=[call("web__screenshot", {})]), assistant("seen")]
        harness, _model, _out = build_harness(
            self.root, replies, mcp_config={"servers": {"web": browserish()}})
        self.addCleanup(harness.close)
        harness.run_user_turn("take a screenshot")
        self.assertLess(len(harness.messages[3]["content"]), 200)


if __name__ == "__main__":
    unittest.main()
