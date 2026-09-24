"""The model client: one HTTP POST, or one SSE stream, plus retries.

micro-harness posts once and raises on any error. That is the first thing a real
harness has to fix, because a single 429 should not end a session. Everything
here is still plain `urllib`, so the retry policy stays visible.
"""

import json
import random
import socket
import time
import urllib.error
import urllib.request


class ModelError(Exception):
    """A failed model call. `retryable` decides whether the loop tries again."""

    def __init__(self, message, retryable=False, retry_after=None, status=None):
        super().__init__(message)
        self.retryable = retryable
        self.retry_after = retry_after
        self.status = status  # the HTTP status, or None for a connection-level failure


class Completion:
    """What a model call returns: the assistant message, verbatim, plus usage."""

    def __init__(self, message, usage=None):
        self.message = message
        self.usage = usage or {}


def with_retries(operation, max_retries=3, sleep=time.sleep, jitter=random.random, out=None,
                 backoff_cap=8, on_retry=None):
    """Run `operation`, retrying only errors that mark themselves retryable.

    `on_retry(error, attempt)` is told about every retry before the sleep, so the
    session log can record it; what it returns is ignored.
    """
    for attempt in range(max_retries + 1):
        try:
            return operation()
        except ModelError as error:
            if not error.retryable or attempt == max_retries:
                raise
            delay = error.retry_after
            if delay is None:
                delay = min(2 ** attempt, backoff_cap) + jitter()
            if out is not None:
                out("model: %s; retrying in %.1fs" % (error, delay))
            if on_retry is not None:
                on_retry(error, attempt + 1)
            sleep(delay)
    raise AssertionError("unreachable")


def merge_tool_call(calls, part):
    """Fold a streamed tool_call delta into the call at its index."""
    index = part.get("index", len(calls))
    while len(calls) <= index:
        calls.append({"id": "", "type": "function",
                      "function": {"name": "", "arguments": ""}})
    target = calls[index]
    if part.get("id"):
        target["id"] = part["id"]
    if part.get("type"):
        target["type"] = part["type"]
    function = part.get("function") or {}
    if function.get("name"):
        target["function"]["name"] += function["name"]
    if function.get("arguments"):
        target["function"]["arguments"] += function["arguments"]


def assemble_message(chunks, on_text=None):
    """Fold SSE chunks into one assistant message.

    Streaming must not change what the loop appends: the result of this function
    has to be identical to what the blocking endpoint would have returned.
    """
    message = {"role": "assistant", "content": ""}
    calls = []
    usage = {}
    for chunk in chunks:
        if chunk.get("usage"):
            usage = chunk["usage"]
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            text = delta.get("content")
            if text:
                message["content"] += text
                if on_text is not None:
                    on_text(text)
            for part in delta.get("tool_calls") or []:
                merge_tool_call(calls, part)
    if calls:
        message["tool_calls"] = calls
    return message, usage


class Model:
    """An OpenAI-shaped chat-completions client."""

    def __init__(self, base_url, model, api_key, timeout=120, max_retries=3,
                 stream=True, out=None, backoff_cap=8, retry_statuses=None,
                 on_retry=None):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout = timeout
        self.max_retries = max_retries
        self.stream = stream
        self.out = out
        self.backoff_cap = backoff_cap
        # None keeps the default rule: 429 and every 5xx are worth another try.
        self.retry_statuses = frozenset(retry_statuses) if retry_statuses else None
        self.on_retry = on_retry

    def complete(self, messages, tools, on_text=None):
        return with_retries(
            lambda: self._attempt(messages, tools, on_text),
            max_retries=self.max_retries,
            out=self.out,
            backoff_cap=self.backoff_cap,
            on_retry=self.on_retry,
        )

    def _attempt(self, messages, tools, on_text):
        if self.stream:
            return self._stream(messages, tools, on_text)
        return self._blocking(messages, tools)

    def _body(self, messages, tools, stream):
        body = {"model": self.model, "messages": messages, "stream": stream}
        if tools:
            body["tools"] = tools
        if stream:
            body["stream_options"] = {"include_usage": True}
        return json.dumps(body).encode()

    def _request(self, messages, tools, stream):
        return urllib.request.Request(
            self.base_url + "/chat/completions",
            data=self._body(messages, tools, stream),
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + self.api_key,
            },
        )

    def _open(self, messages, tools, stream):
        try:
            return urllib.request.urlopen(
                self._request(messages, tools, stream), timeout=self.timeout
            )
        except urllib.error.HTTPError as error:
            body = error.read().decode("utf-8", "replace")
            retry_after = error.headers.get("Retry-After") if error.headers else None
            try:
                retry_after = float(retry_after) if retry_after else None
            except ValueError:
                retry_after = None
            if self.retry_statuses is not None:
                retryable = error.code in self.retry_statuses
            else:
                retryable = error.code == 429 or error.code >= 500
            raise ModelError("HTTP %s: %s" % (error.code, body.strip()),
                             retryable=retryable, retry_after=retry_after,
                             status=error.code)
        except urllib.error.URLError as error:
            raise ModelError("connection failed: %s" % error.reason, retryable=True)
        except (TimeoutError, socket.timeout):
            raise ModelError("request timed out after %ss" % self.timeout, retryable=True)

    def _blocking(self, messages, tools):
        with self._open(messages, tools, False) as response:
            try:
                payload = json.loads(response.read())
            except json.JSONDecodeError as error:
                raise ModelError("model returned invalid JSON: %s" % error)
        choices = payload.get("choices") or []
        if not choices:
            raise ModelError("model returned no choices: %s" % json.dumps(payload)[:400])
        return Completion(choices[0]["message"], payload.get("usage"))

    def _stream(self, messages, tools, on_text):
        message = {"role": "assistant", "content": ""}
        calls = []
        usage = {}
        emitted = False
        try:
            with self._open(messages, tools, True) as response:
                for raw in response:
                    line = raw.decode("utf-8", "replace").strip()
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    if chunk.get("usage"):
                        usage = chunk["usage"]
                    for choice in chunk.get("choices") or []:
                        delta = choice.get("delta") or {}
                        text = delta.get("content")
                        if text:
                            message["content"] += text
                            emitted = True
                            if on_text is not None:
                                on_text(text)
                        for part in delta.get("tool_calls") or []:
                            merge_tool_call(calls, part)
        except (urllib.error.URLError, TimeoutError, socket.timeout, OSError) as error:
            reason = getattr(error, "reason", error)
            # Retrying after the user has seen half an answer would duplicate it.
            raise ModelError("stream failed: %s" % reason, retryable=not emitted)
        if calls:
            message["tool_calls"] = calls
        return Completion(message, usage)
