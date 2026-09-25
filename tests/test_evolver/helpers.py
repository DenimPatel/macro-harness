"""Record real session logs with the scripted model, for the evolver to read."""

import os
import tempfile
import unittest

from macroharness import permissions, session as session_mod
from tests.fake_model import assistant, build_harness, call

__all__ = ["EvolverCase", "assistant", "call", "record"]


def record(root, session_id, turns, rules=None, hooks=None, max_steps=8):
    """Write one session log. `turns` is [(user_text, replies, answers), ...]."""
    sessions = os.path.join(root, ".macroharness", "sessions")
    os.makedirs(sessions, exist_ok=True)
    session = session_mod.Session.at(sessions, session_id)
    replies, answers = [], []
    for _text, turn_replies, turn_answers in turns:
        replies += turn_replies
        answers += turn_answers or []
    harness, _model, _out = build_harness(
        root, replies, rules=rules if rules is not None else permissions.DEFAULT_RULES,
        answers=answers, hooks=hooks, session=session, max_steps=max_steps)
    for text, _replies, _answers in turns:
        harness.run_user_turn(text)
    harness.close()
    return session.path


def ask_session(root, session_id, command="python3 -V", answer="y", text=None):
    return record(root, session_id, [(
        text or "check the python version for %s" % session_id,
        [assistant(tool_calls=[call("run_bash", {"command": command})]), assistant("done")],
        [answer])])


class EvolverCase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = os.path.realpath(os.path.join(self.directory.name, "ws"))
        self.state = os.path.join(self.directory.name, "state")
        os.makedirs(self.root)
