"""Command line entry point: `python3 -m macroharness [options]`."""

import argparse
import json
import os
import sys

from . import context, mcp, permissions, session as session_mod, tools as tools_mod
from .loop import Harness
from .model import Model
from .permissions import PolicyError
from .subagents import AgentError, load_agents, task_tool

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-chat"
STATE_DIR = ".macroharness"
GITIGNORE = "sessions/\nmcp-media/\n"
BANNER = ("macro-harness: the loop plus the layers. "
          "/compact /tokens /session /rules /mcp /agents /exit")
CONTINUE_PROMPT = (
    "Continue the task from where you left off. If earlier context was "
    "compacted, re-read your state files before acting.")


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
    parser.add_argument("--subagent-max-steps", type=int, default=None,
                        help="tool rounds per subagent task (default: min(--max-steps, 8)); "
                             "an agent profile's max_steps overrides it")
    parser.add_argument("--bash-timeout", type=int, default=60,
                        help="seconds before run_bash kills a command (default: 60)")
    parser.add_argument("--prompt-file", default=None,
                        help="read the one-shot prompt from this file (joined before any "
                             "trailing prompt words)")
    parser.add_argument("--rounds", type=int, default=1,
                        help="one-shot mode: after the first turn, send up to N-1 "
                             "'continue' turns, for long unattended runs (default: 1)")
    parser.add_argument("--stop-file", default=None,
                        help="with --rounds: stop before the next turn once this "
                             "workspace-relative file exists")
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

    try:
        agents = load_agents(state)
    except AgentError as error:
        raise PolicyError(str(error))

    containment = tools_mod.Containment(root, timeout=args.bash_timeout,
                                        env_allow=policy.get("env_allow") or ())
    registry = tools_mod.default_registry(containment)

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
        agents=agents,
        subagent_max_steps=args.subagent_max_steps,
    )
    registry.register(task_tool(harness.spawn_subagent, agents))
    harness.mcp_servers = mcp.load_servers(
        registry, load_mcp_config(os.path.join(state, "mcp.json")), root, out=out)
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
    elif command == "/mcp":
        if not harness.mcp_servers:
            out("no MCP servers running (configure .macroharness/mcp.json)")
        for server in harness.mcp_servers:
            out("%s: %d tool(s), timeout %ss" % (
                server.name, len(server.tool_names), server.timeout))
            for name in server.tool_names:
                out("  %s__%s" % (server.name, name))
    elif command == "/agents":
        if not harness.agents:
            out("no agent profiles (add .macroharness/agents/<name>.md)")
        for name in sorted(harness.agents):
            agent = harness.agents[name]
            out("%-14s %s" % (name, agent.description))
            out("%-14s tools: %s; max_steps: %s" % (
                "", ", ".join(agent.tools) if agent.tools else "all",
                agent.max_steps or "default"))
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


def read_prompt(args):
    parts = []
    if args.prompt_file:
        try:
            with open(args.prompt_file, "r", encoding="utf-8") as handle:
                parts.append(handle.read().strip())
        except OSError as error:
            raise PolicyError("cannot read --prompt-file: %s" % error)
    parts.append(" ".join(args.prompt).strip())
    return "\n\n".join(part for part in parts if part)


def run_rounds(harness, prompt, rounds=1, stop_file=None):
    """The one-shot turn, then up to rounds-1 'continue' turns.

    An orchestrator that runs for hours will still end turns now and then (to
    report, or because it hit --max-steps). Rounds re-enter the same session,
    so compaction and the log carry over. A stop file is checked before every
    follow-up, which gives a human a clean way to end a run: `touch` it.
    """
    stop_path = os.path.join(harness.root, stop_file) if stop_file else None
    done = 0
    text = prompt
    while done < max(rounds, 1):
        if done and stop_path and os.path.exists(stop_path):
            harness.out("[stop file %s exists; ending after %d round(s)]" % (stop_file, done))
            break
        if harness._budget_reached():
            break
        if done:
            harness.out("[round %d/%d]" % (done + 1, rounds))
        harness.run_user_turn(text)
        done += 1
        text = CONTINUE_PROMPT
    return done


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        prompt = read_prompt(args)
        harness = build(args)
    except PolicyError as error:
        sys.exit(str(error))
    if not prompt:
        repl(harness)
        return 0
    try:
        run_rounds(harness, prompt, args.rounds, args.stop_file)
    except KeyboardInterrupt:
        harness.out("\n[turn interrupted; the session is kept]")
    finally:
        harness.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
