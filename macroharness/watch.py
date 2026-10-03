"""`mh-watch`: look at a running session from another terminal.

The harness never talks to the viewer. Everything here is read from the
append-only logs the loop already writes, which is why a viewer can attach,
detach and re-attach at any time without the agent noticing:

  sessions/<id>.jsonl        the parent; its "subagent" events say which
                             subagents exist, whether they are still running,
                             and which log is theirs
  sessions/<id>.sub-N.jsonl  one subagent's full transcript, written live

    mh-watch               live table of the latest session's subagents
    mh-watch 3             follow subagent 3: every call, result and reply
    mh-watch main          follow the parent (the orchestrator) itself
    mh-watch --once        print the table once and exit
"""

import argparse
import json
import os
import shutil
import sys
import time

from .session import Session

CLEAR = "\033[H\033[2J"


def read_events(path, offset=0):
    """Complete lines from `offset` on. A half-written tail line is left for
    the next read, so a live log is never misparsed."""
    events = []
    if not os.path.exists(path):
        return events, offset
    with open(path, "r", encoding="utf-8") as handle:
        handle.seek(offset)
        while True:
            line = handle.readline()
            if not line or not line.endswith("\n"):
                break
            offset = handle.tell()
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return events, offset


def resolve_session(sessions_dir, session_id=None):
    return Session.resume(sessions_dir, session_id or "latest")


def subagents(parent_events):
    """Fold the lifecycle events into one record per subagent, in spawn order."""
    table = {}
    for event in parent_events:
        if event.get("t") != "subagent":
            continue
        record = table.setdefault(event["n"], {})
        if event.get("state") == "running":
            record["started"] = event.get("at")
        else:
            record["ended"] = event.get("at")
        record.update(event)
    return [table[number] for number in sorted(table)]


def activity(events):
    """What a transcript is doing right now, from its last events."""
    steps = sum(1 for event in events if event.get("t") == "assistant")
    pending = []
    for event in events:
        kind = event.get("t")
        if kind == "assistant":
            pending = [call for call in event["message"].get("tool_calls") or []]
        elif kind == "tool":
            pending = [call for call in pending
                       if call.get("id") != event.get("tool_call_id")]
    last = events[-1].get("t") if events else None
    if pending:
        return steps, "running " + ", ".join(describe_call(call) for call in pending)
    if last == "assistant":
        return steps, "answered"
    if last in ("tool", "user", "system", "compact"):
        return steps, "waiting on the model"
    return steps, "starting"


def describe_call(call, limit=60):
    function = call.get("function") or {}
    name = function.get("name") or "?"
    try:
        arguments = json.loads(function.get("arguments") or "{}")
    except json.JSONDecodeError:
        arguments = {}
    if not isinstance(arguments, dict):
        arguments = {}
    detail = (arguments.get("command") or arguments.get("path")
              or arguments.get("url") or arguments.get("prompt") or "")
    detail = " ".join(str(detail).split())
    if len(detail) > limit:
        detail = detail[:limit - 3] + "..."
    return "%s %s" % (name, detail) if detail else name


def elapsed(seconds):
    seconds = int(max(seconds, 0))
    if seconds < 60:
        return "%ds" % seconds
    if seconds < 3600:
        return "%dm%02ds" % divmod(seconds, 60)
    hours, rest = divmod(seconds, 3600)
    return "%dh%02dm" % (hours, rest // 60)


def render_table(session, sessions_dir, now=None, width=None):
    now = now or time.time()
    width = width or shutil.get_terminal_size((120, 20)).columns
    parent_events, _ = read_events(session.path)
    records = subagents(parent_events)
    main_steps, main_now = activity(
        [event for event in parent_events if event.get("t") != "subagent"])
    running = sum(1 for record in records if record.get("state") == "running")
    lines = [
        "session %s   %d subagent(s), %d running" % (session.id, len(records), running),
        "main: step %d, %s" % (main_steps, main_now),
        "",
        "%-4s %-20s %-8s %-8s %-8s %s" % ("#", "AGENT", "STATE", "STEP", "TIME", "NOW / TASK"),
    ]
    for record in records:
        events, _ = read_events(os.path.join(sessions_dir, record["log"]))
        steps, now_doing = activity(events)
        state = record.get("state", "?")
        end = record.get("ended") if state != "running" else now
        detail = now_doing if state == "running" else (
            record.get("error") or (record.get("reply") or "").replace("\n", " "))
        row = "%-4s %-20s %-8s %-8s %-8s %s" % (
            record["n"], record.get("agent") or "general", state,
            "%d/%s" % (steps, record.get("max_steps", "?")),
            elapsed((end or now) - (record.get("started") or now)), detail)
        task = "     task: %s" % record.get("prompt", "")
        lines.append(row[:width])
        lines.append(task[:width])
    if not records:
        lines.append("(no subagents yet)")
    lines.append("")
    lines.append("follow one: mh-watch <#>   follow the orchestrator: mh-watch main")
    return "\n".join(lines)


def format_event(event, full=False, result_lines=6):
    """One transcript event as terminal lines."""
    kind = event.get("t")
    out = []
    if kind == "system":
        out.append("system    %s" % first_line(event.get("text", "")))
    elif kind == "user":
        out.append("user      %s" % clip(event.get("text", ""), full, result_lines))
    elif kind == "assistant":
        message = event.get("message") or {}
        if message.get("content"):
            out.append("assistant %s" % clip(message["content"], full, result_lines * 3))
        for call in message.get("tool_calls") or []:
            function = call.get("function") or {}
            out.append("call      %s %s" % (function.get("name"),
                                            clip(function.get("arguments") or "", full, 3)))
    elif kind == "tool":
        timing = "" if event.get("ms") is None else " %dms" % event["ms"]
        out.append("result    %s [%s%s] %s" % (event.get("name"), event.get("decision"),
                                               timing, clip(event.get("content", ""),
                                                            full, result_lines)))
    elif kind == "subagent":
        out.append("subagent  #%s %s %s: %s" % (event.get("n"), event.get("agent") or "general",
                                                event.get("state"), event.get("prompt", "")))
    elif kind == "compact":
        out.append("compact   folded %d messages" % len(event.get("folded") or []))
    elif kind == "budget":
        out.append("budget    stopped at %s/%s tokens" % (event.get("spent"), event.get("limit")))
    return out


def first_line(text):
    return (str(text).strip().splitlines() or [""])[0]


def clip(text, full, max_lines):
    text = str(text).rstrip()
    lines = text.splitlines() or [""]
    if full or len(lines) <= max_lines:
        body = lines
    else:
        body = lines[:max_lines] + ["... (%d more lines; --full shows all)"
                                    % (len(lines) - max_lines)]
    return "\n          ".join(body)


def follow(path, out, is_done, full=False, interval=0.5, once=False):
    """Print a log, then keep printing what is appended until `is_done()`."""
    offset = 0
    while True:
        events, offset = read_events(path, offset)
        for event in events:
            for line in format_event(event, full=full):
                out(line)
        if once or is_done():
            # one last read: the final lines may land just after the state flips
            events, offset = read_events(path, offset)
            for event in events:
                for line in format_event(event, full=full):
                    out(line)
            return
        time.sleep(interval)


def build_parser():
    parser = argparse.ArgumentParser(
        prog="mh-watch",
        description="Watch a macro-harness session and its subagents from another terminal.")
    parser.add_argument("target", nargs="?", default=None,
                        help="a subagent number (3 or sub-3) to follow, or 'main' for "
                             "the orchestrator; omit for the live subagent table")
    parser.add_argument("--workspace", default=None,
                        help="the workspace the agent runs in (default: current directory)")
    parser.add_argument("--session", default=None,
                        help="session id to watch (default: the latest)")
    parser.add_argument("--once", action="store_true",
                        help="print the current state and exit instead of refreshing")
    parser.add_argument("--full", action="store_true",
                        help="when following, print whole tool results and replies")
    parser.add_argument("--interval", type=float, default=1.0,
                        help="refresh interval in seconds (default: 1)")
    return parser


def main(argv=None, out=None):
    args = build_parser().parse_args(argv)
    out = out or (lambda text="": print(text, flush=True))
    root = os.path.realpath(args.workspace or os.getcwd())
    sessions_dir = os.path.join(root, ".macroharness", "sessions")
    try:
        session = resolve_session(sessions_dir, args.session)
    except FileNotFoundError as error:
        sys.exit("mh-watch: %s" % error)

    if args.target is None:
        clear = sys.stdout.isatty() and not args.once
        try:
            while True:
                table = render_table(session, sessions_dir)
                out((CLEAR if clear else "") + table)
                if args.once:
                    return 0
                time.sleep(args.interval)
        except KeyboardInterrupt:
            return 0

    if args.target == "main":
        path = session.path
        is_done = lambda: False  # the orchestrator ends when you stop watching
    else:
        try:
            number = int(args.target.replace("sub-", ""))
        except ValueError:
            sys.exit("mh-watch: target must be a subagent number or 'main'")
        path = os.path.join(sessions_dir, "%s.sub-%d.jsonl" % (session.id, number))
        if not os.path.exists(path):
            known = [str(record["n"]) for record in subagents(read_events(session.path)[0])]
            sys.exit("mh-watch: no subagent %d in %s (known: %s)"
                     % (number, session.id, ", ".join(known) or "none"))

        def is_done():
            records = subagents(read_events(session.path)[0])
            return any(record["n"] == number and record.get("state") != "running"
                       for record in records)

        out("following subagent %d of %s (Ctrl-C to stop)" % (number, session.id))
    try:
        follow(path, out, is_done, full=args.full, interval=args.interval / 2,
               once=args.once)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
