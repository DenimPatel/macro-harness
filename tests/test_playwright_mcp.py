"""The real Playwright MCP server, driven through the loop by a verifier subagent.

Skipped unless MH_PLAYWRIGHT_MCP holds the command that starts the server, e.g.

    MH_PLAYWRIGHT_MCP="npx -y @playwright/mcp@latest --headless --isolated" \
        python3 -m unittest tests.test_playwright_mcp -v

Everything else in the suite runs offline; this one needs Node, the package and
a Chromium. The model is still scripted: what is under test is the harness's
side of the conversation with a real browser server.
"""

import functools
import http.server
import os
import shlex
import subprocess
import sys
import tempfile
import threading
import unittest

from macroharness.subagents import parse_agent
from tests.fake_model import assistant, build_harness, call

COMMAND = os.environ.get("MH_PLAYWRIGHT_MCP", "")

PAGE = """<!doctype html>
<html><head><title>Demo shop</title>
<meta name="viewport" content="width=device-width, initial-scale=1"></head>
<body><h1>Demo shop</h1>
<button id="buy" onclick="document.getElementById('status').textContent='Added to cart'">Buy</button>
<p id="status"></p></body></html>
"""

VERIFIER = """---
description: checks a change in the browser
tools: playwright__*, write_file
max_steps: 12
---
Open the page, exercise the change, take screenshots, write a report.
"""


@unittest.skipUnless(COMMAND, "set MH_PLAYWRIGHT_MCP to run against Playwright MCP")
class PlaywrightMcpTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = os.path.realpath(self.directory.name)
        site = os.path.join(self.root, "site")
        os.makedirs(site)
        with open(os.path.join(site, "index.html"), "w") as handle:
            handle.write(PAGE)
        handler = functools.partial(_QuietHandler, directory=site)
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        self.url = "http://127.0.0.1:%d/" % self.httpd.server_address[1]

    def config(self):
        parts = shlex.split(COMMAND)
        return {"servers": {"playwright": {"command": parts[0], "args": parts[1:],
                                           "timeout": 90}}}

    def test_a_verifier_subagent_drives_the_browser_and_reports(self):
        sub = [
            assistant(tool_calls=[call("playwright__browser_navigate", {"url": self.url})]),
            assistant(tool_calls=[call("playwright__browser_snapshot", {})]),
            assistant(tool_calls=[call("playwright__browser_click",
                                       {"element": "Buy button", "target": "#buy"})]),
            assistant(tool_calls=[call("playwright__browser_take_screenshot",
                                       {"type": "png",
                                        "filename": "artifacts/IMP-001/desktop.png"})]),
            assistant(tool_calls=[call("playwright__browser_resize",
                                       {"width": 390, "height": 844})]),
            assistant(tool_calls=[call("playwright__browser_take_screenshot",
                                       {"type": "png",
                                        "filename": "artifacts/IMP-001/mobile.png"})]),
            assistant(tool_calls=[call("playwright__browser_console_messages",
                                       {"level": "error"})]),
            assistant(tool_calls=[call("write_file", {
                "path": "reports/verify-IMP-001.md",
                "content": "VERDICT: PASS\nclicked Buy, saw 'Added to cart'\n"})]),
            assistant("VERDICT: PASS. Buy works at 1280px and 390px."),
        ]
        replies = ([assistant(tool_calls=[call("task", {
            "prompt": "Verify IMP-001 at %s" % self.url, "agent": "verifier"})])]
                   + sub + [assistant("IMP-001 verified")])
        harness, _model, out = build_harness(
            self.root, replies, max_steps=4, mcp_config=self.config(),
            agents={"verifier": parse_agent("verifier", VERIFIER)}, timeout=60)
        self.addCleanup(harness.close)
        self.assertIsNotNone(harness.registry.get("playwright__browser_navigate"),
                             out.text)

        reply = harness.run_user_turn("verify the Buy button")

        self.assertEqual(reply, "IMP-001 verified")
        self.assertIn("VERDICT: PASS", harness.messages[3]["content"])
        log = os.path.join(harness.sessions_dir, harness.session.id + ".sub-1.jsonl")
        with open(log) as handle:
            transcript = handle.read()
        self.assertIn('heading \\"Demo shop\\"', transcript)  # the snapshot
        self.assertIn("Added to cart", transcript)  # the click took effect
        self.assertNotIn("iVBORw0KGgo", transcript)  # no base64 in context
        for name in ("desktop.png", "mobile.png"):
            path = os.path.join(self.root, "artifacts", "IMP-001", name)
            with open(path, "rb") as handle:
                self.assertEqual(handle.read(4), b"\x89PNG")
        self.assertTrue(os.path.exists(
            os.path.join(self.root, "reports", "verify-IMP-001.md")))


@unittest.skipUnless(COMMAND, "set MH_PLAYWRIGHT_MCP to run against Playwright MCP")
class ImproverDemoTests(unittest.TestCase):
    def test_the_improver_demo_passes_its_own_outcome_checks(self):
        demo = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "examples", "improver", "demo.py")
        result = subprocess.run([sys.executable, demo], capture_output=True, text=True,
                                timeout=300)
        self.assertEqual(result.returncode, 0, result.stdout[-3000:] + result.stderr[-2000:])
        self.assertNotIn("[FAIL]", result.stdout)


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *_args):
        pass


if __name__ == "__main__":
    unittest.main()
