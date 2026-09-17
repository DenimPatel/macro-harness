"""The log is the source of truth: replay, compaction events, and resume."""

import json
import os
import tempfile
import unittest

from macroharness.session import Session, messages_from_events, slugify


class ReplayTests(unittest.TestCase):
    def test_events_replay_into_messages(self):
        events = [
            {"t": "system", "text": "sys"},
            {"t": "user", "text": "hi"},
            {"t": "assistant", "message": {"role": "assistant", "content": "hello"}},
            {"t": "tool", "tool_call_id": "c1", "content": "result"},
            {"t": "budget", "spent": 10, "limit": 5},
            {"t": "rule", "tool": "run_bash", "arg": "git *", "verb": "allow"},
        ]
        self.assertEqual(messages_from_events(events), [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
            {"role": "tool", "tool_call_id": "c1", "content": "result"},
        ])

    def test_a_compact_event_folds_the_recorded_indexes(self):
        events = [
            {"t": "system", "text": "sys"},
            {"t": "user", "text": "one"},
            {"t": "assistant", "message": {"role": "assistant", "content": "two"}},
            {"t": "user", "text": "three"},
            {"t": "compact", "folded": [1, 2], "summary": "notes"},
        ]
        self.assertEqual(messages_from_events(events), [
            {"role": "system", "content": "sys"},
            {"role": "system", "content": "notes"},
            {"role": "user", "content": "three"},
        ])

    def test_an_empty_fold_is_ignored(self):
        events = [
            {"t": "user", "text": "hi"},
            {"t": "compact", "folded": [], "summary": "notes"},
        ]
        self.assertEqual(messages_from_events(events),
                         [{"role": "user", "content": "hi"}])


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.sessions = os.path.join(self.directory.name, "sessions")

    def test_append_then_replay(self):
        session = Session.create(self.sessions, "a first prompt")
        session.append({"t": "system", "text": "sys"})
        session.append({"t": "user", "text": "hi"})
        self.assertEqual(session.messages(), [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hi"},
        ])
        self.assertTrue(session.id.endswith("-a-first-prompt"))

    def test_a_truncated_tail_line_is_skipped(self):
        session = Session.create(self.sessions, "x")
        session.append({"t": "user", "text": "hi"})
        with open(session.path, "a") as handle:
            handle.write('{"t": "user", "text": "trunc')
        self.assertEqual(len(session.messages()), 1)

    def test_resume_latest_ignores_subagent_logs(self):
        Session.at(self.sessions, "20260101-000000-a").append({"t": "user", "text": "a"})
        Session.at(self.sessions, "20260202-000000-b").append({"t": "user", "text": "b"})
        Session.at(self.sessions, "20260303-000000-b.sub-1").append({"t": "user", "text": "c"})
        self.assertEqual(Session.resume(self.sessions, "latest").id, "20260202-000000-b")

    def test_resume_by_id(self):
        Session.at(self.sessions, "20260202-000000-b").append({"t": "user", "text": "b"})
        self.assertEqual(Session.resume(self.sessions, "20260202-000000-b").id,
                         "20260202-000000-b")

    def test_resume_of_a_missing_session_raises(self):
        os.makedirs(self.sessions, exist_ok=True)
        with self.assertRaises(FileNotFoundError):
            Session.resume(self.sessions, "nope")

    def test_resume_with_no_sessions_at_all_raises(self):
        with self.assertRaises(FileNotFoundError):
            Session.resume(self.sessions, "latest")

    def test_events_are_json_lines(self):
        session = Session.create(self.sessions, "x")
        session.append({"t": "user", "text": "hi"})
        with open(session.path) as handle:
            self.assertEqual(json.loads(handle.readline()), {"t": "user", "text": "hi"})


class SlugTests(unittest.TestCase):
    def test_slugify_is_filename_safe(self):
        self.assertEqual(slugify("Fix the Flappy Bird!"), "fix-the-flappy-bird")
        self.assertEqual(slugify("!!!", limit=5), "")
        self.assertEqual(len(slugify("a" * 100)), 32)


if __name__ == "__main__":
    unittest.main()
