"""Episodes and signals: what the evolver reads out of a session log."""

from evolver import signals, split, traces
from tests.test_evolver.helpers import EvolverCase, ask_session, assistant, call, record


def sid(number):
    return "20250101-0000%02d-s" % number


class TraceTests(EvolverCase):
    def test_turns_carry_messages_tool_events_and_ends(self):
        record(self.root, sid(1), [
            ("list files", [assistant(tool_calls=[call("run_bash", {"command": "ls"})]),
                            assistant("two files")], None),
            ("thanks", [assistant("welcome")], None),
        ])
        (episode,) = traces.load(self.root)
        self.assertEqual(len(episode.turns), 2)
        first = episode.turns[0]
        self.assertEqual(first.user_text, "list files")
        self.assertEqual(len(first.assistant_messages), 2)
        self.assertEqual(first.tool_events[0]["arg"], "ls")
        self.assertEqual(first.stopped, "none")
        self.assertEqual(first.arguments_for(first.tool_events[0]["tool_call_id"]),
                         {"command": "ls"})
        self.assertIn("trace_version", episode.header)

    def test_network_commands_and_mcp_tools_taint_a_turn(self):
        record(self.root, sid(1), [
            ("fetch", [assistant(tool_calls=[call("run_bash",
                                                  {"command": "curl https://x.example"})]),
                       assistant("ok")], ["y"]),
            ("plain", [assistant(tool_calls=[call("run_bash", {"command": "ls"})]),
                       assistant("ok")], None),
        ])
        (episode,) = traces.load(self.root)
        self.assertEqual([turn.tainted for turn in episode.turns], [True, False])


class SignalTests(EvolverCase):
    def test_asks_reconstruct_the_answer_and_the_arguments(self):
        ask_session(self.root, sid(1), answer="y")
        ask_session(self.root, sid(2), command="python3 -c pass", answer="d")
        asks = signals.asks(traces.load(self.root))
        self.assertEqual([(a["arguments"]["command"], a["answer"]) for a in asks],
                         [("python3 -V", "y"), ("python3 -c pass", "d")])

    def test_corrections_label_the_turn_before_the_pushback(self):
        record(self.root, sid(1), [
            ("rename the file", [assistant("renamed")], None),
            ("no, the other one", [assistant("fixed")], None),
        ])
        self.assertEqual(signals.corrections(traces.load(self.root)),
                         [{"session": sid(1), "turn": 0}])

    def test_restatements_must_span_sessions(self):
        for number in (1, 2):
            record(self.root, sid(number), [("always use tabs for indentation",
                                             [assistant("ok")], None)])
        record(self.root, sid(3), [("something else entirely here",
                                    [assistant("ok")], None)])
        clusters = signals.restatements(traces.load(self.root))
        self.assertEqual(len(clusters), 1)
        self.assertEqual(len(clusters[0]["refs"]), 2)

    def test_error_signatures_blur_numbers_and_paths(self):
        one = signals.error_signature("exit 1\nstdout:\n\nstderr:\n  File /a/b.py, line 3")
        two = signals.error_signature("exit 1\nstdout:\n\nstderr:\n  File /c/d.py, line 9")
        self.assertEqual(one, two)

    def test_metrics_count_what_happened(self):
        ask_session(self.root, sid(1))
        record(self.root, sid(2), [("spin", [assistant(tool_calls=[
            call("run_bash", {"command": "true"})])] * 2, ["y", "y"])], max_steps=2)
        metrics = signals.metrics(traces.load(self.root))
        self.assertEqual(metrics["sessions"], 2)
        self.assertEqual(metrics["asks"], 3)
        self.assertEqual(metrics["stops"], 1)
        self.assertEqual(metrics["asks_per_session"], 1.5)


class SplitTests(EvolverCase):
    def test_the_split_is_by_session_hash_and_stable(self):
        for number in range(1, 7):
            ask_session(self.root, sid(number))
        mining, held = split.split(traces.load(self.root))
        self.assertEqual([e.id for e in held], [sid(2)])
        self.assertEqual(len(mining), 5)
        self.assertIsInstance(mining, split.MiningSet)
        self.assertIsInstance(held, split.HeldOut)

    def test_the_newest_is_held_out_when_the_hash_picks_none(self):
        for number in (1, 3, 4):
            ask_session(self.root, sid(number))
        _mining, held = split.split(traces.load(self.root))
        self.assertEqual([e.id for e in held], [sid(4)])

    def test_a_pinned_session_stays_held_out_as_sessions_arrive(self):
        from evolver import pipeline
        from evolver.store import Store
        store = Store(self.root, root=self.state)
        for number in (1, 3, 4):
            ask_session(self.root, sid(number))
        _mining, held = pipeline.split_for(store, traces.load(self.root))
        self.assertEqual([e.id for e in held], [sid(4)])
        for number in (5, 6):
            ask_session(self.root, sid(number))
        mining, held = pipeline.split_for(store, traces.load(self.root))
        self.assertIn(sid(4), [e.id for e in held])
        self.assertNotIn(sid(4), [e.id for e in mining])

    def test_a_plain_list_is_not_a_mining_set(self):
        with self.assertRaises(TypeError):
            split.require_mining_set(traces.load(self.root))
