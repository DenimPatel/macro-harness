"""strategy.json: data proposes, code bounds."""

import os
import tempfile
import unittest

from macroharness import __main__ as cli
from macroharness import context, permissions, strategy
from tests.fake_model import Collector, ScriptedModel, assistant


class ClampTests(unittest.TestCase):
    def test_values_inside_the_caps_pass_through(self):
        settings, notes = strategy.clamp({"retry": {"max": 2, "on_status": [429, 503]},
                                          "max_steps": 20, "parallel_tools": 2})
        self.assertEqual(notes, [])
        self.assertEqual(settings["retry"], {"max": 2, "backoff_cap": 8,
                                             "on_status": [429, 503]})
        self.assertEqual((settings["max_steps"], settings["parallel_tools"]), (20, 2))

    def test_values_past_a_cap_are_pulled_back_and_reported(self):
        settings, notes = strategy.clamp({"max_steps": 100000, "parallel_tools": 500,
                                          "compact_at_ratio": 2})
        self.assertEqual(settings["max_steps"], 64)
        self.assertEqual(settings["parallel_tools"], 8)
        self.assertEqual(settings["compact_at_ratio"], 0.9)
        # Three single-value clamps, plus the combined cap: 64 steps x 4 attempts.
        self.assertEqual(len(notes), 4)
        self.assertEqual(settings["retry"]["max"], 2)

    def test_retries_times_steps_is_capped_even_when_each_is_in_range(self):
        settings, notes = strategy.clamp({"retry": {"max": 6}, "max_steps": 64})
        self.assertLessEqual((settings["retry"]["max"] + 1) * settings["max_steps"],
                             strategy.MAX_RETRY_STEP_PRODUCT)
        self.assertTrue(any("retries x steps" in note for note in notes))

    def test_endpoint_and_key_are_not_settings(self):
        _settings, notes = strategy.clamp({"base_url": "https://evil.example",
                                           "api_key_env": "X"})
        self.assertEqual(len(notes), 2)
        self.assertTrue(all("not a strategy setting" in note for note in notes))

    def test_non_retryable_statuses_are_dropped(self):
        settings, notes = strategy.clamp({"retry": {"on_status": [401, 429]}})
        self.assertEqual(settings["retry"]["on_status"], [429])
        self.assertTrue(notes)

    def test_bad_types_are_errors(self):
        with self.assertRaises(strategy.StrategyError):
            strategy.clamp({"max_steps": "lots"})
        with self.assertRaises(strategy.StrategyError):
            strategy.clamp({"retry": {"on_status": "429"}})


class LoadTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.state = self.directory.name

    def test_no_file_is_defaults(self):
        self.assertEqual(strategy.load(self.state, out=Collector()), strategy.DEFAULTS)

    def test_an_untrusted_file_is_ignored_without_a_human(self):
        strategy.write(strategy.strategy_path(self.state), {"max_steps": 5})
        out = Collector()
        settings = strategy.load(self.state, interactive=False, out=out)
        self.assertEqual(settings["max_steps"], 16)
        self.assertIn("not trusted", out.text)

    def test_a_trusted_file_applies_and_editing_it_revokes_trust(self):
        path = strategy.strategy_path(self.state)
        strategy.write(path, {"max_steps": 5})
        settings = strategy.load(self.state, prompt_fn=lambda _q: True, out=Collector())
        self.assertEqual(settings["max_steps"], 5)
        self.assertEqual(strategy.load(self.state, interactive=False,
                                       out=Collector())["max_steps"], 5)
        strategy.write(path, {"max_steps": 6})
        self.assertEqual(strategy.load(self.state, interactive=False,
                                       out=Collector())["max_steps"], 16)


class WiringTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = os.path.realpath(self.directory.name)
        self.state = os.path.join(self.root, ".macroharness")
        os.makedirs(self.state)

    def build(self, *extra):
        args = cli.build_parser().parse_args(["--workspace", self.root] + list(extra))
        harness = cli.build(args, model=ScriptedModel([assistant("hi")]),
                            trust_prompt=lambda _q: True, out=Collector())
        self.addCleanup(harness.close)
        return harness

    def test_strategy_reaches_the_loop(self):
        strategy.write(strategy.strategy_path(self.state),
                       {"max_steps": 7, "parallel_tools": 2, "compact_at_ratio": 0.5})
        harness = self.build()
        self.assertEqual(harness.max_steps, 7)
        self.assertEqual(harness.parallel_tools, 2)
        self.assertEqual(harness.compact_at, int(context.CONTEXT_LIMIT * 0.5))

    def test_the_command_line_wins(self):
        strategy.write(strategy.strategy_path(self.state), {"max_steps": 7})
        self.assertEqual(self.build("--max-steps", "3").max_steps, 3)

    def test_the_default_is_unchanged_without_a_file(self):
        harness = self.build()
        self.assertEqual(harness.max_steps, 16)
        self.assertFalse(os.path.exists(strategy.strategy_path(self.state)))
        self.assertIsNone(permissions.trusted_digests(self.state).get(strategy.TRUST_KEY))


if __name__ == "__main__":
    unittest.main()
