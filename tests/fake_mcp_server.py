"""A tiny MCP server over stdio, used by test_mcp.py.

It implements exactly the slice macro-harness speaks: initialize, tools/list and
tools/call, with one read-only `echo` tool.

`--browserish` adds the behaviour a browser server such as Playwright MCP shows:
a `screenshot` tool that returns a base64 PNG block, a `slow` tool, a schema
carrying "$schema", a server-to-client request before each reply, and a flood
of stderr logging that would fill an undrained pipe.
"""

import base64
import json
import sys
import time

BROWSERISH = "--browserish" in sys.argv
# The smallest valid PNG: 1x1, transparent.
PNG = base64.b64encode(bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082"
)).decode()

TOOLS = [{
    "name": "echo",
    "description": "Echo the given text back.",
    "inputSchema": {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    },
    "annotations": {"readOnlyHint": True},
}]
EXTRA_TOOLS = [{
    "name": "screenshot",
    "description": "Return a picture of the page.",
    "inputSchema": {"$schema": "http://json-schema.org/draft-07/schema#",
                    "type": "object", "properties": {}},
    "annotations": {"readOnlyHint": True},
}, {
    "name": "slow",
    "description": "Sleep, then answer.",
    "inputSchema": {"type": "object", "properties": {"seconds": {"type": "number"}}},
}]


def respond(message):
    method = message.get("method")
    if method == "initialize":
        result = {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
                  "serverInfo": {"name": "fake", "version": "0"}}
    elif method == "tools/list":
        result = {"tools": TOOLS + (EXTRA_TOOLS if BROWSERISH else [])}
    elif method == "tools/call":
        params = message.get("params") or {}
        arguments = params.get("arguments") or {}
        if params.get("name") == "screenshot":
            result = {"content": [{"type": "text", "text": "took a screenshot"},
                                  {"type": "image", "mimeType": "image/png", "data": PNG}]}
        elif params.get("name") == "slow":
            time.sleep(float(arguments.get("seconds", 0)))
            result = {"content": [{"type": "text", "text": "slept"}]}
        else:
            result = {"content": [{"type": "text",
                                   "text": "echo: %s" % arguments.get("text", "")}]}
    else:
        return {"jsonrpc": "2.0", "id": message.get("id"),
                "error": {"code": -32601, "message": "no such method"}}
    return {"jsonrpc": "2.0", "id": message.get("id"), "result": result}


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        message = json.loads(line)
        if message.get("id") is None:
            continue  # a notification needs no reply
        if "method" not in message:
            continue  # the client's answer to our own request
        if BROWSERISH:
            # Reuse the client's id on purpose: a client that matched on id
            # alone would take this request for its reply.
            sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": message["id"],
                                         "method": "roots/list", "params": {}}) + "\n")
            sys.stderr.write("log line " + "x" * 200 + "\n")
            sys.stderr.write(("noise " * 40 + "\n") * 400)
            sys.stderr.flush()
        sys.stdout.write(json.dumps(respond(message)) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
