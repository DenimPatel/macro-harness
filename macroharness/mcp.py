"""A minimal MCP client: stdio JSON-RPC, tools/list and tools/call only.

micro-harness hardcodes its tools. MCP is what happens when you stop doing that.
This is the smallest useful slice of the protocol: start a server, ask what it
can do, and register those tools in the same registry the built-ins live in, so
they flow through the same permission pipeline with no special casing.

Tools that return pictures (a browser's screenshot) send base64 image blocks.
Those never reach the transcript: they are written under
.macroharness/mcp-media/ and the result says where, so a 200KB screenshot costs
one line of context instead of eight thousand characters of noise.

Not implemented, on purpose: resources, prompts, sampling, notifications other
than id-matched replies, and reconnection after a crash. A request the server
sends us is answered: `ping` with an empty result, anything else with
"method not found", so a server never waits on us.
"""

import base64
import collections
import itertools
import json
import os
import queue
import subprocess
import threading
import time

from .tools import Tool

PROTOCOL_VERSION = "2024-11-05"
DESCRIPTION_LIMIT = 400
DEFAULT_TIMEOUT = 20
STDERR_TAIL = 20
MEDIA_DIR = os.path.join(".macroharness", "mcp-media")
EXTENSIONS = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif",
              "image/webp": ".webp", "audio/wav": ".wav", "audio/mpeg": ".mp3"}


class McpError(Exception):
    pass


def default_config():
    return {"servers": {}}


class McpServer:
    """One stdio server. Requests are synchronous and id-matched."""

    def __init__(self, name, spec, root, timeout=None):
        self.name = name
        self.spec = spec
        self.root = root
        # mcp.json may set "timeout" per server: a browser navigation can take
        # far longer than an echo.
        self.timeout = timeout or spec.get("timeout") or DEFAULT_TIMEOUT
        self.process = None
        self.tool_names = []
        self._inbox = queue.Queue()
        self._next_id = 0
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._stderr = collections.deque(maxlen=STDERR_TAIL)
        self._media = itertools.count(1)

    def start(self):
        command = [self.spec["command"]] + list(self.spec.get("args") or [])
        env = dict(os.environ)
        env.update(self.spec.get("env") or {})
        self.process = subprocess.Popen(
            command, cwd=self.root, env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1,
        )
        threading.Thread(target=self._read_stdout, daemon=True).start()
        # An undrained stderr pipe fills at 64KB and the server blocks on its
        # next log line. Keep only the tail, for error messages.
        threading.Thread(target=self._read_stderr, daemon=True).start()
        self._request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "macro-harness", "version": "0.1.0"},
        })
        self._notify("notifications/initialized", {})
        result = self._request("tools/list", {}) or {}
        tools = result.get("tools") or []
        self.tool_names = [tool.get("name") for tool in tools]
        return tools

    def _read_stdout(self):
        for line in self.process.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(message, dict):
                continue
            if message.get("method") and message.get("id") is not None:
                self._answer_server_request(message)
                continue
            self._inbox.put(message)

    def _read_stderr(self):
        for line in self.process.stderr:
            self._stderr.append(line.rstrip())

    def stderr_tail(self):
        return "\n".join(self._stderr)

    def _answer_server_request(self, message):
        """The server asked us something. We advertise no capabilities, so
        everything but ping is method-not-found, answered at once."""
        if message["method"] == "ping":
            reply = {"jsonrpc": "2.0", "id": message["id"], "result": {}}
        else:
            reply = {"jsonrpc": "2.0", "id": message["id"],
                     "error": {"code": -32601, "message": "method not found"}}
        try:
            self._send(reply)
        except (OSError, ValueError):
            pass

    def _send(self, payload):
        # The reader thread answers server requests while a caller may be
        # mid-request, so writes get their own lock.
        with self._write_lock:
            self.process.stdin.write(json.dumps(payload) + "\n")
            self.process.stdin.flush()

    def _request(self, method, params):
        with self._lock:
            self._next_id += 1
            request_id = self._next_id
            self._send({"jsonrpc": "2.0", "id": request_id,
                        "method": method, "params": params})
            deadline = time.monotonic() + self.timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise self._timeout_error(method)
                try:
                    message = self._inbox.get(timeout=remaining)
                except queue.Empty:
                    raise self._timeout_error(method)
                if message.get("id") != request_id:
                    continue  # a notification or a stale reply
                if message.get("error"):
                    raise McpError("%s: %s" % (self.name, message["error"]))
                return message.get("result")

    def _timeout_error(self, method):
        text = "%s: timed out after %ss waiting for %s" % (self.name, self.timeout, method)
        if self.process is not None and self.process.poll() is not None:
            text = "%s: server exited (code %s) during %s" % (
                self.name, self.process.returncode, method)
        tail = self.stderr_tail()
        if tail:
            text += "\nserver stderr:\n" + tail
        return McpError(text)

    def _notify(self, method, params):
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def call(self, tool_name, arguments):
        result = self._request("tools/call",
                               {"name": tool_name, "arguments": arguments}) or {}
        parts = [self._render(block) for block in result.get("content") or []]
        text = "\n".join(parts)
        if result.get("isError"):
            return "error: %s" % text
        return text

    def _render(self, block):
        kind = block.get("type")
        if kind == "text":
            return block.get("text") or ""
        if kind in ("image", "audio") and block.get("data"):
            return self._save_media(block)
        if kind == "resource":
            resource = block.get("resource") or {}
            if resource.get("text") is not None:
                return resource["text"]
            if resource.get("blob"):
                return self._save_media({"data": resource["blob"],
                                         "mimeType": resource.get("mimeType", "")})
        return json.dumps(block)

    def _save_media(self, block):
        mime = block.get("mimeType") or "application/octet-stream"
        try:
            data = base64.b64decode(block["data"], validate=False)
        except (ValueError, TypeError) as error:
            return "[%s block could not be decoded: %s]" % (mime, error)
        directory = os.path.join(self.root, MEDIA_DIR, self.name)
        os.makedirs(directory, exist_ok=True)
        name = "%s-%d%s" % (time.strftime("%Y%m%d-%H%M%S"), next(self._media),
                            EXTENSIONS.get(mime, ".bin"))
        with open(os.path.join(directory, name), "wb") as handle:
            handle.write(data)
        relative = os.path.join(MEDIA_DIR, self.name, name)
        return "[%s saved to %s (%d bytes)]" % (mime, relative, len(data))

    def close(self):
        if self.process is None:
            return
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            try:
                if stream is not None and not stream.closed:
                    stream.close()
            except OSError:
                pass


def _as_tool(server, spec):
    name = "%s__%s" % (server.name, spec.get("name", "unknown"))
    read_only = bool((spec.get("annotations") or {}).get("readOnlyHint"))

    def call(**arguments):
        return server.call(spec.get("name"), arguments)

    # Some providers reject JSON Schema's "$schema" key inside tool parameters.
    schema = dict(spec.get("inputSchema") or {"type": "object", "properties": {}})
    schema.pop("$schema", None)
    return Tool(
        name,
        (spec.get("description") or "")[:DESCRIPTION_LIMIT],
        schema,
        call,
        read_only=read_only,
    )


def load_servers(registry, config, root, out=print, attempts=2):
    """Start every configured server and register its tools. Fail soft."""
    opened = []
    for name, spec in (config or {}).get("servers", {}).items():
        tools = None
        for attempt in range(1, attempts + 1):
            server = McpServer(name, spec, root)
            try:
                tools = server.start()
                break
            except Exception as error:
                server.close()
                if attempt == attempts:
                    out("mcp: %s unavailable: %s" % (name, error))
        if tools is None:
            continue
        for spec_tool in tools:
            registry.register(_as_tool(server, spec_tool))
        opened.append(server)
        out("mcp: %s -> %d tool(s)" % (name, len(tools)))
    return opened
