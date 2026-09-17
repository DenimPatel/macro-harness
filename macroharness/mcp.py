"""A minimal MCP client: stdio JSON-RPC, tools/list and tools/call only.

micro-harness hardcodes its tools. MCP is what happens when you stop doing that.
This is the smallest useful slice of the protocol: start a server, ask what it
can do, and register those tools in the same registry the built-ins live in, so
they flow through the same permission pipeline with no special casing.

Not implemented, on purpose: resources, prompts, sampling, notifications other
than id-matched replies, and reconnection after a crash.
"""

import json
import os
import queue
import subprocess
import threading
import time

from .tools import Tool

PROTOCOL_VERSION = "2024-11-05"
DESCRIPTION_LIMIT = 400


class McpError(Exception):
    pass


def default_config():
    return {"servers": {}}


class McpServer:
    """One stdio server. Requests are synchronous and id-matched."""

    def __init__(self, name, spec, root, timeout=20):
        self.name = name
        self.spec = spec
        self.root = root
        self.timeout = timeout
        self.process = None
        self._inbox = queue.Queue()
        self._next_id = 0
        self._lock = threading.Lock()

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
        self._request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "macro-harness", "version": "0.1.0"},
        })
        self._notify("notifications/initialized", {})
        result = self._request("tools/list", {}) or {}
        return result.get("tools") or []

    def _read_stdout(self):
        for line in self.process.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                self._inbox.put(json.loads(line))
            except json.JSONDecodeError:
                continue

    def _send(self, payload):
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
                    raise McpError("%s: timed out waiting for %s" % (self.name, method))
                try:
                    message = self._inbox.get(timeout=remaining)
                except queue.Empty:
                    raise McpError("%s: timed out waiting for %s" % (self.name, method))
                if message.get("id") != request_id:
                    continue  # a notification or a stale reply
                if message.get("error"):
                    raise McpError("%s: %s" % (self.name, message["error"]))
                return message.get("result")

    def _notify(self, method, params):
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def call(self, tool_name, arguments):
        result = self._request("tools/call",
                               {"name": tool_name, "arguments": arguments}) or {}
        parts = []
        for block in result.get("content") or []:
            if block.get("type") == "text":
                parts.append(block.get("text") or "")
            else:
                parts.append(json.dumps(block))
        text = "\n".join(parts)
        if result.get("isError"):
            return "error: %s" % text
        return text

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

    return Tool(
        name,
        (spec.get("description") or "")[:DESCRIPTION_LIMIT],
        spec.get("inputSchema") or {"type": "object", "properties": {}},
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
