"""Retries and streaming: the two layers that sit inside the model client."""

import unittest

from macroharness import model as model_mod
from macroharness.model import Model, ModelError, with_retries


class FakeResponse:
    """Enough of an HTTP response for the streaming path."""

    def __init__(self, lines):
        self._lines = lines

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def __iter__(self):
        return iter([(line + "\n").encode() for line in self._lines])


class RetryTests(unittest.TestCase):
    def test_retryable_errors_are_retried(self):
        attempts = []

        def operation():
            attempts.append(1)
            if len(attempts) < 3:
                raise ModelError("boom", retryable=True)
            return "ok"

        result = with_retries(operation, max_retries=3,
                              sleep=lambda _delay: None, jitter=lambda: 0)
        self.assertEqual(result, "ok")
        self.assertEqual(len(attempts), 3)

    def test_non_retryable_errors_raise_immediately(self):
        attempts = []

        def operation():
            attempts.append(1)
            raise ModelError("bad request", retryable=False)

        with self.assertRaises(ModelError):
            with_retries(operation, sleep=lambda _delay: None)
        self.assertEqual(len(attempts), 1)

    def test_retries_stop_at_the_limit(self):
        attempts = []

        def operation():
            attempts.append(1)
            raise ModelError("boom", retryable=True)

        with self.assertRaises(ModelError):
            with_retries(operation, max_retries=2,
                         sleep=lambda _delay: None, jitter=lambda: 0)
        self.assertEqual(len(attempts), 3)

    def test_retry_after_is_honoured_over_the_backoff(self):
        slept = []
        calls = []

        def operation():
            calls.append(1)
            if len(calls) == 1:
                raise ModelError("slow down", retryable=True, retry_after=7)
            return "ok"

        with_retries(operation, sleep=slept.append, jitter=lambda: 0)
        self.assertEqual(slept, [7])

    def test_backoff_grows(self):
        slept = []

        def operation():
            raise ModelError("boom", retryable=True)

        with self.assertRaises(ModelError):
            with_retries(operation, max_retries=3,
                         sleep=slept.append, jitter=lambda: 0)
        self.assertEqual(slept, [1, 2, 4])


class AssembleTests(unittest.TestCase):
    def test_text_deltas_accumulate(self):
        chunks = [{"choices": [{"delta": {"content": "Hel"}}]},
                  {"choices": [{"delta": {"content": "lo"}}]}]
        message, usage = model_mod.assemble_message(chunks)
        self.assertEqual(message["content"], "Hello")
        self.assertNotIn("tool_calls", message)
        self.assertEqual(usage, {})

    def test_a_tool_call_split_across_chunks_survives(self):
        chunks = [
            {"choices": [{"delta": {"tool_calls": [
                {"index": 0, "id": "c1", "type": "function",
                 "function": {"name": "run_bash", "arguments": ""}}]}}]},
            {"choices": [{"delta": {"tool_calls": [
                {"index": 0, "function": {"arguments": '{"comm'}}]}}]},
            {"choices": [{"delta": {"tool_calls": [
                {"index": 0, "function": {"arguments": 'and": "ls"}'}}]}}]},
        ]
        message, _usage = model_mod.assemble_message(chunks)
        call = message["tool_calls"][0]
        self.assertEqual(call["id"], "c1")
        self.assertEqual(call["type"], "function")
        self.assertEqual(call["function"]["name"], "run_bash")
        self.assertEqual(call["function"]["arguments"], '{"command": "ls"}')

    def test_two_tool_calls_are_kept_apart_by_index(self):
        chunks = [{"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "a", "function": {"name": "read_file", "arguments": "{}"}},
            {"index": 1, "id": "b", "function": {"name": "read_file", "arguments": "{}"}},
        ]}}]}]
        message, _usage = model_mod.assemble_message(chunks)
        self.assertEqual([call["id"] for call in message["tool_calls"]], ["a", "b"])

    def test_usage_chunks_are_captured(self):
        _message, usage = model_mod.assemble_message(
            [{"choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 2}}])
        self.assertEqual(usage["prompt_tokens"], 5)

    def test_on_text_sees_every_delta(self):
        seen = []
        model_mod.assemble_message(
            [{"choices": [{"delta": {"content": "a"}}]},
             {"choices": [{"delta": {"content": "b"}}]}],
            on_text=seen.append)
        self.assertEqual(seen, ["a", "b"])


class StreamTests(unittest.TestCase):
    def test_the_streaming_path_returns_the_blocking_shape(self):
        client = Model("http://example.invalid", "m", "k", stream=True)
        client._open = lambda *_args: FakeResponse([
            'data: {"choices":[{"delta":{"content":"hi"}}]}',
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c1",'
            '"type":"function","function":{"name":"run_bash","arguments":"{}"}}]}}]}',
            'data: {"choices":[],"usage":{"prompt_tokens":9,"completion_tokens":1}}',
            "data: [DONE]",
        ])
        completion = client.complete([{"role": "user", "content": "x"}], [])
        self.assertEqual(completion.message["role"], "assistant")
        self.assertEqual(completion.message["content"], "hi")
        self.assertEqual(completion.message["tool_calls"][0]["id"], "c1")
        self.assertEqual(completion.usage["prompt_tokens"], 9)

    def test_invalid_json_lines_are_skipped(self):
        client = Model("http://example.invalid", "m", "k", stream=True)
        client._open = lambda *_args: FakeResponse([
            "data: not json at all",
            'data: {"choices":[{"delta":{"content":"ok"}}]}',
            "data: [DONE]",
        ])
        completion = client.complete([{"role": "user", "content": "x"}], [])
        self.assertEqual(completion.message["content"], "ok")


class BodyTests(unittest.TestCase):
    def test_tools_and_stream_options_are_only_sent_when_relevant(self):
        import json as json_module
        client = Model("http://example.invalid", "m", "k", stream=False)
        body = json_module.loads(client._body([{"role": "user", "content": "x"}], [], False))
        self.assertNotIn("tools", body)
        self.assertNotIn("stream_options", body)
        self.assertFalse(body["stream"])

        streaming = json_module.loads(
            client._body([{"role": "user", "content": "x"}], [{"type": "function"}], True))
        self.assertIn("tools", streaming)
        self.assertEqual(streaming["stream_options"], {"include_usage": True})


if __name__ == "__main__":
    unittest.main()
