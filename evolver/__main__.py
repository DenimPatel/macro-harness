"""`mh-evolve`: the offline evolver's command line.

    mh-evolve run                 ingest -> mine -> gate -> inbox
    mh-evolve review              walk the inbox: y applies, n rejects, s skips
    mh-evolve stats               metrics per window, and per artifact in force
    mh-evolve revert ID           take one applied change back out
    mh-evolve replayset add SESSION:TURN --check "python3 -m unittest -q"

Every command takes --workspace (default: the current directory) and --state
(default: ~/.macroharness-evolver/<hash of the workspace>).
"""

import argparse
import json
import os
import sys

from . import pipeline, split, stats, traces
from .apply import ApplyError, apply, revert
from .inbox import render, review
from .proposal import PENDING
from .store import Store


def build_parser():
    parser = argparse.ArgumentParser(
        prog="mh-evolve",
        description="The offline evolver for macro-harness: mines session logs, gates "
                    "proposals against replayed sessions, and queues survivors for review.")
    parser.add_argument("--workspace", default=None,
                        help="the macro-harness workspace (default: the current directory)")
    parser.add_argument("--state", default=None,
                        help="evolver state directory (default: outside the workspace)")
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("ingest", help="read session logs and record a metrics snapshot")
    commands.add_parser("mine", help="run the miners and keep what passes static checks")
    for name, text in (("gate", "replay-test every mined proposal"),
                       ("run", "ingest, mine and gate in one go")):
        command = commands.add_parser(name, help=text)
        command.add_argument("--live", action="store_true",
                             help="also run tier 2: replay-set cases against the real model")
        command.add_argument("--budget", type=int, default=50000,
                             help="token budget for tier 2 (default: 50000)")
        command.add_argument("--runs", type=int, default=1,
                             help="tier 2 runs per case and variant (default: 1)")
        command.add_argument("--api-key-env", default="DEEPSEEK_API_KEY")
    commands.add_parser("inbox", help="show proposals waiting for review")
    command = commands.add_parser("review", help="accept or reject waiting proposals")
    command.add_argument("--accept-all-untainted", action="store_true",
                         help="apply every untainted proposal without asking (for scripts)")
    command = commands.add_parser("apply", help="apply one waiting proposal by id")
    command.add_argument("id")
    command = commands.add_parser("revert", help="take one applied change back out")
    command.add_argument("id")
    command = commands.add_parser("stats", help="metrics and artifact activations")
    command.add_argument("--window", type=int, default=20)
    command = commands.add_parser("replayset", help="manage the held-out replay cases")
    actions = command.add_subparsers(dest="action", required=True)
    add = actions.add_parser("add", help="promote a held-out turn to a named case")
    add.add_argument("ref", help="SESSION:TURN")
    add.add_argument("--check", required=True, help="shell command; exit 0 means passed")
    add.add_argument("--name", default=None)
    actions.add_parser("list")
    remove = actions.add_parser("remove")
    remove.add_argument("name")
    return parser


def _live(args):
    if not getattr(args, "live", False):
        return None
    return {"api_key_env": args.api_key_env, "budget": args.budget, "runs": args.runs}


def _replayset(store, args, out):
    cases = store.replayset()
    if args.action == "list":
        if not cases:
            out("replay set is empty")
        for name, case in sorted(cases.items()):
            out("%-24s %s turn %d  check: %s" % (name, case["session"], case["turn"],
                                                 case["check"]))
        return 0
    if args.action == "remove":
        if cases.pop(args.name, None) is None:
            out("no case named %s" % args.name)
            return 1
        store.save_replayset(cases)
        return 0
    session_id, _, turn = args.ref.rpartition(":")
    episodes = traces.by_id(traces.load(store.workspace))
    episode = episodes.get(session_id)
    if episode is None or not turn.isdigit() or int(turn) >= len(episode.turns):
        out("no turn %s in the session logs" % args.ref)
        return 1
    if not split.is_held_out(session_id):
        _mining, held = pipeline.split_for(store, list(episodes.values()))
        if session_id not in {e.id for e in held}:
            out("%s is in the mining set; replay cases must come from held-out sessions, "
                "or the proposer could fit to them" % session_id)
            return 1
    name = args.name or "%s-%s" % (session_id[-12:], turn)
    cases[name] = {"session": session_id, "turn": int(turn), "check": args.check,
                   "prompt": episode.turns[int(turn)].user_text}
    store.save_replayset(cases)
    out("added %s" % name)
    return 0


def main(argv=None, out=print, answer_fn=None):
    args = build_parser().parse_args(argv)
    workspace = os.path.realpath(args.workspace or os.getcwd())
    store = Store(workspace, root=args.state)

    if args.command == "ingest":
        _episodes, snapshot = pipeline.ingest(store)
        out(json.dumps(snapshot, indent=2, sort_keys=True))
    elif args.command == "mine":
        pipeline.mine(store, traces.load(workspace), out=out)
    elif args.command == "gate":
        episodes = traces.load(workspace)
        live = _live(args)
        if live:
            live = pipeline.live_config(store, episodes, live["api_key_env"],
                                        live["budget"], live["runs"])
        pipeline.gate(store, episodes, live=live, out=out)
    elif args.command == "run":
        pipeline.run(store, live=_live(args), out=out)
    elif args.command == "inbox":
        pending = store.inbox(status=PENDING)
        if not pending:
            out("inbox is empty")
        for proposal in pending:
            out(render(proposal))
    elif args.command == "review":
        review(store, answer_fn=answer_fn, accept_all_untainted=args.accept_all_untainted,
               out=out)
    elif args.command == "apply":
        proposal = store.get(args.id)
        if proposal is None or proposal.get("status") != PENDING:
            out("%s is not a proposal waiting for review" % args.id)
            return 1
        try:
            apply(store, proposal)
        except ApplyError as error:
            out("could not apply %s: %s" % (args.id, error))
            return 1
        out("applied %s" % args.id)
    elif args.command == "revert":
        try:
            revert(store, args.id)
        except ApplyError as error:
            out("could not revert %s: %s" % (args.id, error))
            return 1
        out("reverted %s" % args.id)
    elif args.command == "stats":
        out(stats.report(traces.load(workspace), store.active_artifacts(), args.window))
    elif args.command == "replayset":
        return _replayset(store, args, out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
