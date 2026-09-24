"""Control-flow parameters the history says are wrong for this user.

Two patterns, both mechanical:

- turns that keep running out of tool rounds -> raise `max_steps`;
- model calls that keep using every retry and still failing -> one more retry.

Each proposal stays inside the harness's hard caps, including the cap on
retries times steps; a value that would need clamping is not proposed at all.
Compaction thresholds are deliberately not mined: a session that compacts often
is a symptom with several opposite cures, and a miner that guesses between them
is a miner that guesses.
"""

from ..adapters import macroharness as adapter
from ..proposal import evidence_from, make
from ..split import require_mining_set
from ..traces import by_id


def _effective(context):
    settings, _notes = adapter.clamp_strategy(context.artifacts["strategy"] or {})
    return settings


def _turns(mining, refs):
    episodes = by_id(mining)
    return [episodes[ref["session"]].turns[ref["turn"]] for ref in refs]


def mine(mining, context):
    require_mining_set(mining)
    settings = _effective(context)
    caps = context.artifacts["hard_caps"]
    thresholds = context.thresholds
    proposals = []
    all_turns = [turn for episode in mining for turn in episode.turns]

    stopped = [turn for turn in all_turns if turn.stopped == "max_steps"]
    if (len(stopped) >= thresholds["stops_min"] and all_turns
            and len(stopped) / len(all_turns) >= thresholds["stops_share"]):
        retries = settings["retry"]["max"]
        ceiling = min(caps["max_steps"][1], 200 // (retries + 1))
        target = min(int(settings["max_steps"] * 1.5) + 1, ceiling)
        if target > settings["max_steps"]:
            proposals.append(make(
                "strategy", {"max_steps": target}, miner="strategy:max_steps",
                predicts_fix=[turn.ref for turn in stopped],
                risks=["a turn that is looping can now spend %d rounds instead of %d"
                       % (target, settings["max_steps"]),
                       "more tokens per turn when the model does not converge"],
                check="tier 1: the predicted turns no longer stop at the step limit; "
                      "no new errors on held-out sessions. Whether the extra rounds "
                      "finish the task is a tier 2 question",
                evidence=evidence_from(stopped, "stopped at %d rounds" % settings["max_steps"]),
                turns=stopped,
                effect="raise max_steps from %d to %d (%d of %d turns hit the limit)"
                       % (settings["max_steps"], target, len(stopped), len(all_turns)),
            ))

    current_max = settings["retry"]["max"]
    exhausted = [(turn, retry) for turn in all_turns for retry in turn.retries
                 if int(retry.get("attempt") or 0) >= current_max]
    if len(exhausted) >= thresholds["retry_exhaustion_min"]:
        target = current_max + 1
        steps = settings["max_steps"]
        if target <= caps["retry.max"][1] and (target + 1) * steps <= 200:
            turns = []
            for turn, _retry in exhausted:
                if turn not in turns:
                    turns.append(turn)
            statuses = sorted({str(retry.get("status")) for _turn, retry in exhausted})
            proposals.append(make(
                "strategy", {"retry": {"max": target}}, miner="strategy:retry",
                predicts_fix=[turn.ref for turn in turns],
                risks=["a hard outage now takes one more backoff before it is reported"],
                check="tier 1 cannot reproduce provider errors, so it only checks for "
                      "regressions on held-out sessions; tier 2 is the real test",
                evidence=evidence_from(turns, "used all %d retries (status %s)"
                                       % (current_max, ", ".join(statuses))),
                turns=turns,
                effect="retry failed model calls up to %d times instead of %d"
                       % (target, current_max),
            ))
    return proposals
