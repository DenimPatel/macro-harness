"""The trace an offline evolver reads: header, outcomes, turn ends, retries, uses.

None of these events may change what a resumed session rebuilds, and none of
them may change a decision. They only describe what happened.
"""

import os
import tempfile
import unittest

from macroharness import __main__ as cli
from macroharness import contract
from macroharness.model import ModelError, with_retries
from macroharness.session import Session
from tests.fake_model import Collector, ScriptedModel, assistant, build_harness, call


def events_of(harness, kind):
    return [event for event in harness.session.events() if event.get("t") == kind]


class TraceEventTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = os.path.realpath(self.directory.name)

    def test_header_follows_the_system_prompt_and_does_not_change_replay(self):
        replies = [assistant(tool_calls=[call("run_bash", {"command": "echo hi"})]),
                   assistant("done")]
        harness, _model, _out = build_harness(self.root, replies)
        harness.run_user_turn("go")
        events = harness.session.events()
        self.assertEqual([e["t"] for e in events[:2]], ["system", contract.HEADER])
        self.assertEqual(events[1]["trace_version"], contract.TRACE_VERSION)
        self.assertFalse(events[1]["resumed"])
        self.assertEqual(harness.session.messages(), harness.messages)

    def test_a_resumed_session_gets_a_second_header_and_rebuilds_identically(self):
        harness, _model, _out = build_harness(self.root, [assistant("one")])
        harness.run_user_turn("first")
        before = list(harness.messages)
        resumed, _model, _out = build_harness(
            self.root, [], session=Session(harness.session.path))
        self.assertEqual(resumed.messages, before)
        headers = events_of(resumed, contract.HEADER)
        self.assertEqual([h["resumed"] for h in headers], [False, True])

    def test_tool_events_carry_the_argument_and_the_outcome(self):
        replies = [
            assistant(tool_calls=[
                call("run_bash", {"command": "echo ok"}, call_id="a"),
                call("read_file", {"path": "missing.txt"}, call_id="b"),
                call("write_file", {"path": "x.pem", "content": "k"}, call_id="c"),
                call("nope", {}, call_id="d"),
            ]),
            assistant("done"),
        ]
        rules = [{"tool": "write_file", "arg": "*.pem", "verb": "deny"},
                 {"tool": "*", "arg": "*", "verb": "allow"}]
        harness, _model, _out = build_harness(self.root, replies, rules=rules)
        harness.run_user_turn("go")
        tools = events_of(harness, "tool")
        self.assertEqual([t["arg"] for t in tools[:3]],
                         ["echo ok", "missing.txt", "x.pem"])
        self.assertEqual([t["outcome"] for t in tools],
                         [contract.OK, contract.ERROR, contract.DENIED, contract.ERROR])

    def test_a_blocked_call_is_recorded_as_blocked(self):
        replies = [assistant(tool_calls=[call("write_file", {"path": "a.txt", "content": "x"})]),
                   assistant("done")]
        hooks = [{"event": "pre_tool", "tool": "write_file", "run": "exit 1",
                  "blocking": True}]
        harness, _model, _out = build_harness(self.root, replies, hooks=hooks)
        harness.run_user_turn("go")
        self.assertEqual(events_of(harness, "tool")[0]["outcome"], contract.BLOCKED)

    def test_turn_end_records_why_the_turn_stopped(self):
        loop = [assistant(tool_calls=[call("run_bash", {"command": "true"})])] * 2
        harness, _model, _out = build_harness(self.root, loop, max_steps=2)
        harness.run_user_turn("spin")
        end = events_of(harness, contract.TURN_END)[-1]
        self.assertEqual(end["stopped"], contract.STOPPED_MAX_STEPS)
        self.assertEqual(end["steps"], 2)

        other = os.path.join(self.root, "other")
        os.makedirs(other)
        harness, _model, _out = build_harness(
            other, [assistant("fine", usage={"prompt_tokens": 50, "completion_tokens": 5})])
        harness.run_user_turn("hello")
        self.assertEqual(events_of(harness, contract.TURN_END)[-1]["stopped"],
                         contract.STOPPED_NONE)
        harness.budget = 10
        harness.run_user_turn("again")
        self.assertEqual(events_of(harness, contract.TURN_END)[-1]["stopped"],
                         contract.STOPPED_BUDGET)

    def test_retries_are_recorded_with_their_status(self):
        harness, _model, _out = build_harness(self.root, [])
        failures = [ModelError("HTTP 529: busy", retryable=True, status=529)]

        def operation():
            if failures:
                raise failures.pop()
            return "ok"

        result = with_retries(operation, sleep=lambda _s: None, jitter=lambda: 0,
                              on_retry=harness.record_retry)
        self.assertEqual(result, "ok")
        retry = events_of(harness, contract.RETRY)[0]
        self.assertEqual((retry["status"], retry["attempt"]), (529, 1))


class ArtifactUseTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = os.path.realpath(self.directory.name)

    def test_an_evolved_rule_and_hook_log_their_use_and_change_nothing(self):
        rule = {"tool": "run_bash", "arg": "echo *", "verb": "allow"}
        hook = {"event": "post_tool", "tool": "run_bash", "arg": "*", "run": "true"}
        evolved = {"r1": {"kind": contract.POLICY_RULE, "match": rule},
                   "h1": {"kind": contract.HOOK, "match": hook}}
        replies = [assistant(tool_calls=[call("run_bash", {"command": "echo hi"})]),
                   assistant("done")]
        harness, _model, _out = build_harness(
            self.root, replies, rules=[rule], hooks=[hook], evolved=evolved)
        harness.run_user_turn("go")
        self.assertEqual([e["id"] for e in events_of(harness, contract.ARTIFACT_USE)],
                         ["r1", "h1"])
        self.assertEqual(events_of(harness, "tool")[0]["outcome"], contract.OK)

    def test_an_evolved_tool_logs_its_use(self):
        from macroharness.tools import Tool
        tool = Tool("stamp", "d", {"type": "object", "properties": {}},
                    lambda: "stamped", read_only=True)
        replies = [assistant(tool_calls=[call("stamp", {}, call_id="a"),
                                         call("read_file", {"path": "x"}, call_id="b")]),
                   assistant("done")]
        harness, _model, _out = build_harness(
            self.root, replies, extra_tools=[tool],
            evolved={"t1": {"kind": contract.TOOL_DEF, "match": {"name": "stamp"}}})
        harness.run_user_turn("go")
        self.assertEqual([e["id"] for e in events_of(harness, contract.ARTIFACT_USE)], ["t1"])

    def test_an_unlabelled_rule_logs_nothing(self):
        replies = [assistant(tool_calls=[call("run_bash", {"command": "echo hi"})]),
                   assistant("done")]
        harness, _model, _out = build_harness(self.root, replies)
        harness.run_user_turn("go")
        self.assertEqual(events_of(harness, contract.ARTIFACT_USE), [])


class CliHeaderTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = os.path.realpath(self.directory.name)

    def test_build_writes_a_header_describing_the_harness_state(self):
        args = cli.build_parser().parse_args(["--workspace", self.root, "--model", "m1"])
        harness = cli.build(args, model=ScriptedModel([assistant("hi")]),
                            trust_prompt=lambda _q: True, out=Collector())
        self.addCleanup(harness.close)
        header = events_of(harness, contract.HEADER)[0]
        self.assertEqual(header["model"], "m1")
        self.assertIn("policy.json", header["artifacts"])
        self.assertEqual(header["harness_digest"], contract.digest(header["artifacts"]))
        self.assertEqual(header["base_url_host"], "api.deepseek.com")

    def test_the_banner_mentions_waiting_proposals(self):
        state = os.path.join(self.root, ".macroharness")
        os.makedirs(state)
        contract.write_evolved_index(state, {"artifacts": {}, "inbox_pending": 2})
        args = cli.build_parser().parse_args(["--workspace", self.root])
        harness = cli.build(args, model=ScriptedModel([]),
                            trust_prompt=lambda _q: True, out=Collector())
        out = Collector()
        with unittest.mock.patch("builtins.input", side_effect=EOFError):
            cli.repl(harness, out=out)
        self.assertIn("2 evolver proposal(s) waiting", out.text)


if __name__ == "__main__":
    unittest.main()
