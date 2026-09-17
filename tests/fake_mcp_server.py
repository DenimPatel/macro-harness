"""A tiny MCP server over stdio, used by test_mcp.py.

It implements exactly the slice macro-harness speaks: initialize, tools/list and
tools/call, with one read-only `echo` tool.
"""

import json
import sys

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


def respond(message):
    method = message.get("method")
    if method == "initialize":
        result = {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
                  "serverInfo": {"name": "fake", "version": "0"}}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        arguments = (message.get("params") or {}).get("arguments") or {}
        result = {"content": [{"type": "text", "text": "echo: %s" % arguments.get("text", "")}]}
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
        sys.stdout.write(json.dumps(respond(message)) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
