"""Command line entry point: `python3 -m macroharness [options]`."""

import argparse
import json
import os
import sys

from . import (context, extensions, hooks as hooks_mod, mcp, permissions,
               session as session_mod, tools as tools_mod)
from .loop import Harness
from .model import Model
from .permissions import PolicyError
from .subagents import task_tool

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-chat"
STATE_DIR = ".macroharness"
GITIGNORE = "sessions/\n"
BANNER = ("macro-harness: the loop plus the layers. "
          "/compact /tokens /session /rules /tools /hooks /exit")


def build_parser():
    parser = argparse.ArgumentParser(
        prog="macroharness",
        description="An agent harness you can still read: micro-harness plus its missing layers.")
    parser.add_argument("--workspace", default=None,
                        help="directory the agent is confined to (default: the current directory)")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--api-key-env", default="DEEPSEEK_API_KEY")
    parser.add_argument("--max-steps", type=int, default=16,
                        help="tool rounds per user turn (default: 16)")
    parser.add_argument("--budget", type=int, default=None,
                        help="prompt+completion token budget for the session")
    parser.add_argument("--resume", nargs="?", const="latest", default=None,
                        metavar="SESSION", help="resume a session id, or the latest one")
    parser.add_argument("--no-stream", action="store_true",
                        help="use the blocking endpoint instead of SSE")
    parser.add_argument("--non-interactive", action="store_true",
                        help="resolve every 'ask' rule to deny instead of prompting")
    parser.add_argument("--no-evolve", action="store_true",
                        help="do not register define_tool and define_hook, so the "
                             "harness cannot extend itself this session")
    parser.add_argument("prompt", nargs="*",
                        help="one-shot prompt; when omitted, start the REPL")
    return parser


def state_paths(root):
    state = os.path.join(root, STATE_DIR)
    return state, os.path.join(state, "sessions")


def load_mcp_config(path):
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(mcp.default_config(), handle, indent=2)
        return mcp.default_config()
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError) as error:
        raise PolicyError("cannot read %s: %s" % (path, error))


def stream_writer():
    def write(text):
        sys.stdout.write(text)
        sys.stdout.flush()
    return write


def build(args, model=None, trust_prompt=None, out=print):
    """Assemble a harness. `model` is injectable so tests can run offline."""
    root = os.path.realpath(args.workspace or os.getcwd())
    state, sessions_dir = state_paths(root)
    os.makedirs(sessions_dir, exist_ok=True)
    gitignore = os.path.join(state, ".gitignore")
    if not os.path.exists(gitignore):
        with open(gitignore, "w", encoding="utf-8") as handle:
            handle.write(GITIGNORE)

    policy_file = permissions.ensure_policy(state)
    policy = permissions.load_policy(policy_file)
    interactive = not args.non_interactive
    permissions.trust_policy(state, policy_file, prompt_fn=trust_prompt,
                             interactive=interactive, out=out)

    console = permissions.Permissions(policy, policy_file, state_dir=state,
                                      interactive=interactive, out=out)

    if args.resume:
        try:
            session = session_mod.Session.resume(sessions_dir, args.resume)
        except FileNotFoundError as error:
            raise PolicyError(str(error))
    else:
        session = session_mod.Session.create(sessions_dir)

    containment = tools_mod.Containment(root, env_allow=policy.get("env_allow") or ())
    registry = tools_mod.default_registry(containment)
    hooks = hooks_mod.Hooks.load(containment, state, interactive=interactive, out=out)

    on_text = None
    if model is None:
        api_key = os.environ.get(args.api_key_env)
        if not api_key:
            raise PolicyError("error: %s is not set (export %s=...)"
                              % (args.api_key_env, args.api_key_env))
        on_text = None if args.no_stream else stream_writer()
        model = Model(args.base_url, args.model, api_key,
                      stream=not args.no_stream, out=out)

    harness = Harness(
        model=model,
        registry=registry,
        permissions=console,
        session=session,
        accounting=context.Accounting(model_name=args.model),
        root=root,
        max_steps=args.max_steps,
        budget=args.budget,
        interactive=interactive,
        sessions_dir=sessions_dir,
        out=out,
        on_text=on_text,
        hooks=hooks,
    )
    registry.register(task_tool(harness.spawn_subagent))
    harness.mcp_servers = mcp.load_servers(
        registry, load_mcp_config(os.path.join(state, "mcp.json")), root, out=out)

    # Extensions register last, so they see every name already taken and cannot
    # shadow a built-in, a subagent tool or an MCP tool.
    extensions.load_all(registry, containment, state, interactive=interactive, out=out)
    if not args.no_evolve:
        registry.register(extensions.define_tool_tool(registry, containment, state))
        registry.register(hooks_mod.define_hook_tool(hooks))
    return harness


def meta(harness, line, out):
    """Handle a /command. Returns True when the REPL should exit."""
    command = line.split()[0]
    if command in ("/exit", "/quit"):
        return True
    if command == "/compact":
        harness.compact_context()
    elif command == "/tokens":
        out(harness.accounting.report())
        out("context now: ~%d tokens (compaction at ~%d)"
            % (context.estimate_tokens(harness.messages), context.COMPACT_AT))
    elif command == "/session":
        out("session: %s" % harness.session.id)
        out("file: %s" % harness.session.path)
        out("messages: %d" % len(harness.messages))
        out("subagents: %d" % harness.sub_count)
    elif command == "/rules":
        out("policy: %s" % harness.permissions.path)
        for rule in harness.permissions.rules:
            out("  %-5s %-30s %s" % (rule["verb"], rule["tool"], rule["arg"]))
    elif command == "/tools":
        for name in harness.registry.names():
            tool = harness.registry.get(name)
            out("  %-24s %s%s" % (name, "[read-only] " if tool.read_only else "",
                                  tool.description.splitlines()[0][:80]))
    elif command == "/hooks":
        harness.hooks.reload()
        if not harness.hooks.hooks:
            out("no hooks (%s)" % (harness.hooks.path or "no file"))
        else:
            out("hooks: %s" % harness.hooks.path)
            for hook in harness.hooks.hooks:
                out("  %s" % hooks_mod.describe(hook))
    else:
        out("unknown command %s" % command)
    return False


def repl(harness, out=print):
    out(BANNER)
    while True:
        try:
            line = input("\nyou> ")
        except (EOFError, KeyboardInterrupt):
            out("")
            break
        line = line.strip()
        if not line:
            continue
        if line.startswith("/"):
            if meta(harness, line, out):
                break
            continue
        try:
            harness.run_user_turn(line)
        except KeyboardInterrupt:
            out("\n[turn interrupted; the session is kept]")
    harness.close()


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        harness = build(args)
    except PolicyError as error:
        sys.exit(str(error))
    prompt = " ".join(args.prompt).strip()
    if not prompt:
        repl(harness)
        return 0
    try:
        harness.run_user_turn(prompt)
    except KeyboardInterrupt:
        harness.out("\n[turn interrupted; the session is kept]")
    finally:
        harness.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
