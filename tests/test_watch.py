"""mh-watch: the subagent table and the follow view, read from live logs."""

import os
import tempfile
import threading
import unittest

from macroharness import watch
from macroharness.subagents import parse_agent
from tests.fake_model import Collector, RoutingModel, assistant, build_harness, call


class RecursionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = os.path.realpath(self.directory.name)

    def test_a_subagent_cannot_see_or_call_task(self):
        replies = [
            assistant(tool_calls=[call("task", {"prompt": "outer"})]),
            assistant(tool_calls=[call("task", {"prompt": "inner"})]),  # the subagent tries
            assistant("could not delegate"),
            assistant("done"),
        ]
        harness, model, _out = build_harness(self.root, replies)

        harness.run_user_turn("go")

        sub_tools = [tool["function"]["name"] for tool in model.requests[1]["tools"]]
        self.assertNotIn("task", sub_tools)
        self.assertIn("Your tools are: read_file, write_file, edit_file, run_bash.",
                      model.requests[1]["messages"][0]["content"])
        self.assertEqual(harness.sub_count, 1)  # nothing was spawned from inside
        log = os.path.join(harness.sessions_dir, harness.session.id + ".sub-1.jsonl")
        with open(log) as handle:
            self.assertIn("unknown tool 'task'", handle.read())

    def test_an_agent_profile_cannot_grant_task_back(self):
        agents = {"sneaky": parse_agent("sneaky", "---\ntools: task, run_bash\n---\n")}
        replies = [
            assistant(tool_calls=[call("task", {"prompt": "x", "agent": "sneaky"})]),
            assistant("ok"),
            assistant("done"),
        ]
        harness, model, _out = build_harness(self.root, replies, agents=agents)
        harness.run_user_turn("go")
        self.assertEqual([tool["function"]["name"] for tool in model.requests[1]["tools"]],
                         ["run_bash"])


class WatchTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = os.path.realpath(self.directory.name)

    def test_the_table_shows_running_subagents_and_what_they_are_doing(self):
        snapshots = []
        lock = threading.Lock()
        holder = {}

        def on_call(key, messages):
            # recon-b's second model call happens after its run_bash finished;
            # recon-a's first call happens while its siblings run. Snapshot the
            # table from inside the run, as a second terminal would.
            if key == "recon-b" and len(messages) == 4:
                with lock:
                    snapshots.append(watch.render_table(
                        holder["harness"].session, holder["harness"].sessions_dir, width=200))

        model = RoutingModel({
            "orchestrate": [
                assistant(tool_calls=[
                    call("task", {"prompt": "recon-a: classify", "agent": "recon"}, "c1"),
                    call("task", {"prompt": "recon-b: runbook"}, "c2"),
                ]),
                assistant("planned"),
            ],
            "recon-a": [assistant("A: library")],
            "recon-b": [
                assistant(tool_calls=[call("run_bash", {"command": "echo tests pass"})]),
                assistant("B: unittest"),
            ],
        }, on_call=on_call)
        agents = {"recon": parse_agent("recon", "---\ndescription: looks\nmax_steps: 5\n---\n")}
        harness, _model, _out = build_harness(self.root, [], model=model, agents=agents)
        holder["harness"] = harness

        harness.run_user_turn("orchestrate the recon")

        live = snapshots[0]
        self.assertIn("running", live)
        self.assertIn("main: step 1, running task recon-a: classify, task recon-b: runbook", live)
        # recon-b's first step and its run_bash are logged; its second model
        # call is the one in flight.
        self.assertRegex(live, r"\n2\s+general\s+running\s+1/8\s+\S+\s+waiting on the model")

        final = watch.render_table(harness.session, harness.sessions_dir, width=200)
        self.assertIn("2 subagent(s), 0 running", final)
        self.assertRegex(final, r"\n1\s+recon\s+done\s+1/5 .*A: library")
        self.assertRegex(final, r"\n2\s+general\s+done\s+2/8 .*B: unittest")
        self.assertIn("task: recon-a: classify", final)
        self.assertIn("main: step 2, answered", final)

    def test_activity_names_the_call_in_flight(self):
        events = [
            {"t": "system", "text": "s"},
            {"t": "user", "text": "u"},
            {"t": "assistant", "message": {"role": "assistant", "tool_calls": [
                call("run_bash", {"command": "npm test -- --watch=false"}, "a"),
                call("read_file", {"path": "src/app.ts"}, "b")]}},
            {"t": "tool", "tool_call_id": "b", "name": "read_file", "content": "x"},
        ]
        steps, now = watch.activity(events)
        self.assertEqual(steps, 1)
        self.assertEqual(now, "running run_bash npm test -- --watch=false")
        self.assertEqual(watch.activity(events[:2])[1], "waiting on the model")
        self.assertEqual(watch.activity([])[1], "starting")

    def test_a_half_written_line_is_left_for_the_next_read(self):
        path = os.path.join(self.root, "log.jsonl")
        with open(path, "w") as handle:
            handle.write('{"t": "user", "text": "a"}\n{"t": "user", "te')
        events, offset = watch.read_events(path)
        self.assertEqual(len(events), 1)
        with open(path, "a") as handle:
            handle.write('xt": "b"}\n')
        more, _ = watch.read_events(path, offset)
        self.assertEqual(more, [{"t": "user", "text": "b"}])

    def run_once(self):
        replies = [
            assistant(tool_calls=[call("task", {"prompt": "count files"})]),
            assistant(tool_calls=[call("run_bash", {"command": "ls"})]),
            assistant("3 files"),
            assistant("done"),
        ]
        harness, _model, _out = build_harness(self.root, replies)
        harness.run_user_turn("go")
        return harness

    def test_main_prints_the_table_once(self):
        harness = self.run_once()
        out = Collector()
        self.assertEqual(watch.main(["--workspace", self.root, "--once"], out=out), 0)
        self.assertIn(harness.session.id, out.text)
        self.assertIn("1 subagent(s), 0 running", out.text)

    def test_main_follows_one_subagent_transcript(self):
        self.run_once()
        out = Collector()
        watch.main(["--workspace", self.root, "1", "--interval", "0.05"], out=out)
        self.assertIn("following subagent 1", out.text)
        self.assertIn("user      count files", out.text)
        self.assertIn("call      run_bash", out.text)
        self.assertIn("result    run_bash [allow", out.text)
        self.assertIn("assistant 3 files", out.text)

    def test_follow_waits_for_a_live_subagent_then_stops(self):
        harness, _model, _out = build_harness(self.root, [])
        harness.session.append({"t": "system", "text": "s"})
        sub_log = os.path.join(harness.sessions_dir, harness.session.id + ".sub-1.jsonl")
        harness.session.append({"t": "subagent", "n": 1, "agent": "", "state": "running",
                                "log": os.path.basename(sub_log), "prompt": "p",
                                "max_steps": 8, "at": 0})
        with open(sub_log, "w") as handle:
            handle.write('{"t": "user", "text": "first"}\n')

        def finish():
            with open(sub_log, "a") as handle:
                handle.write('{"t": "assistant", "message": {"content": "late reply"}}\n')
            harness.session.append({"t": "subagent", "n": 1, "state": "done",
                                    "log": os.path.basename(sub_log), "at": 1})

        timer = threading.Timer(0.3, finish)
        timer.start()
        self.addCleanup(timer.cancel)
        out = Collector()
        watch.main(["--workspace", self.root, "1", "--interval", "0.05"], out=out)
        self.assertIn("user      first", out.text)
        self.assertIn("assistant late reply", out.text)

    def test_unknown_subagent_and_missing_sessions_exit_clearly(self):
        self.run_once()
        with self.assertRaises(SystemExit) as caught:
            watch.main(["--workspace", self.root, "9"], out=Collector())
        self.assertIn("known: 1", str(caught.exception.code))
        empty = tempfile.mkdtemp(dir=self.root)
        with self.assertRaises(SystemExit) as caught:
            watch.main(["--workspace", empty, "--once"], out=Collector())
        self.assertIn("no sessions", str(caught.exception.code))

    def test_lifecycle_events_do_not_change_replay(self):
        harness = self.run_once()
        roles = [message["role"] for message in harness.session.messages()]
        self.assertEqual(roles, ["system", "user", "assistant", "tool", "assistant"])


if __name__ == "__main__":
    unittest.main()
