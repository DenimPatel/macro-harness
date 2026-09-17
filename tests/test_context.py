"""Accounting, fold boundaries, and compaction replay."""

import unittest

from macroharness import context, session as session_mod
from tests.fake_model import ScriptedModel, assistant


def conversation():
    return [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "one"},
        {"role": "assistant", "content": "two"},
        {"role": "user", "content": "three"},
        {"role": "assistant", "content": "four"},
        {"role": "user", "content": "five"},
        {"role": "assistant", "content": "six"},
        {"role": "user", "content": "seven"},
        {"role": "assistant", "content": "eight"},
    ]


def conversation_with_a_tool_pair():
    return [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "build it"},
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": "c1", "type": "function",
                         "function": {"name": "run_bash", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "exit 0"},
        {"role": "tool", "tool_call_id": "c2", "content": "exit 0"},
        {"role": "assistant", "content": "five"},
        {"role": "user", "content": "six"},
        {"role": "assistant", "content": "seven"},
        {"role": "user", "content": "eight"},
        {"role": "assistant", "content": "nine"},
    ]


def padded(messages, size=400):
    """Long messages, so compaction visibly reduces the token count."""
    return [dict(message, content=(message.get("content") or "") + "x" * size)
            for message in messages]


class EstimateTests(unittest.TestCase):
    def test_chars_to_tokens(self):
        self.assertEqual(context.estimate_tokens([{"role": "user", "content": "a" * 800}]), 200)

    def test_tool_call_arguments_count(self):
        with_call = [{"role": "assistant", "content": "",
                      "tool_calls": [{"function": {"arguments": "x" * 40}}]}]
        self.assertGreater(context.estimate_tokens(with_call), 0)


class FoldBoundaryTests(unittest.TestCase):
    def test_short_conversations_are_not_folded(self):
        self.assertIsNone(context.fold_boundary(conversation()[:6]))

    def test_boundary_keeps_the_recent_messages(self):
        self.assertEqual(context.fold_boundary(conversation()), 3)

    def test_boundary_never_splits_a_tool_pair(self):
        boundary = context.fold_boundary(conversation_with_a_tool_pair())
        self.assertEqual(boundary, 5)
        folded = conversation_with_a_tool_pair()[1:boundary]
        self.assertIn("tool_calls", folded[1])
        self.assertEqual([m["role"] for m in folded], ["user", "assistant", "tool", "tool"])


class CompactTests(unittest.TestCase):
    def test_compaction_folds_the_old_part(self):
        messages = padded(conversation())
        model = ScriptedModel([assistant("handover notes")])
        accounting = context.Accounting()

        new_messages, event = context.compact(messages, model, accounting)

        self.assertEqual(event["folded"], [1, 2])
        self.assertEqual(new_messages[0], messages[0])
        self.assertIn("handover notes", new_messages[1]["content"])
        self.assertEqual(new_messages[2:], messages[3:])
        self.assertLess(event["tokens_after"], event["tokens_before"])
        self.assertEqual(accounting.calls, 1)

    def test_compaction_is_skipped_when_there_is_nothing_safe_to_fold(self):
        messages = conversation()[:4]
        new_messages, event = context.compact(
            messages, ScriptedModel([]), context.Accounting())
        self.assertIsNone(event)
        self.assertEqual(new_messages, messages)

    def test_compaction_is_skipped_when_the_budget_is_gone(self):
        accounting = context.Accounting()
        accounting.prompt_tokens = 100
        messages, event = context.compact(
            conversation(), ScriptedModel([]), accounting, budget=10)
        self.assertIsNone(event)
        self.assertEqual(messages, conversation())

    def test_replaying_a_compaction_reproduces_the_live_list(self):
        messages = conversation()
        model = ScriptedModel([assistant("handover notes")])
        new_messages, event = context.compact(messages, model, context.Accounting())

        events = [{"t": "system", "text": "sys"}]
        for message in messages[1:]:
            if message["role"] == "assistant":
                events.append({"t": "assistant", "message": message})
            elif message["role"] == "tool":
                events.append({"t": "tool", "tool_call_id": message["tool_call_id"],
                               "content": message["content"]})
            else:
                events.append({"t": "user", "text": message["content"]})
        events.append(event)

        self.assertEqual(session_mod.messages_from_events(events), new_messages)

    def test_compaction_keeps_a_tool_pair_whole(self):
        messages = conversation_with_a_tool_pair()
        new_messages, event = context.compact(
            messages, ScriptedModel([assistant("notes")]), context.Accounting())
        kept_roles = [m["role"] for m in new_messages]
        self.assertEqual(kept_roles, ["system", "system", "assistant", "user",
                                      "assistant", "user", "assistant"])


class AccountingTests(unittest.TestCase):
    def test_provider_usage_wins_over_the_estimate(self):
        accounting = context.Accounting("deepseek-chat")
        accounting.add({"prompt_tokens": 100, "completion_tokens": 20},
                       [{"role": "user", "content": "ignored"}])
        self.assertEqual((accounting.prompt_tokens, accounting.completion_tokens), (100, 20))

    def test_estimates_are_used_when_usage_is_missing(self):
        accounting = context.Accounting()
        accounting.add({}, [{"role": "user", "content": "a" * 40}], assistant("b" * 40))
        self.assertEqual(accounting.prompt_tokens, 10)
        self.assertEqual(accounting.completion_tokens, 10)

    def test_cost_is_reported_for_known_models_only(self):
        accounting = context.Accounting("deepseek-chat")
        accounting.prompt_tokens, accounting.completion_tokens = 1_000_000, 1_000_000
        self.assertAlmostEqual(accounting.cost(), 1.37)
        self.assertIsNone(context.Accounting("mystery").cost())

    def test_report_mentions_the_totals(self):
        accounting = context.Accounting("deepseek-chat")
        accounting.add({"prompt_tokens": 10, "completion_tokens": 5}, [])
        report = accounting.report()
        self.assertIn("total: 15", report)
        self.assertIn("estimated cost", report)


if __name__ == "__main__":
    unittest.main()
