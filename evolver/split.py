"""The line between what proposes and what judges.

Sessions are split once, by a hash of their id, into a mining set the miners
read and a held-out set the gate replays. Hashing keeps the assignment stable
as new sessions arrive. While there are too few sessions for the hash to pick
any, the newest is held out instead -- and every session ever held out is
pinned by the caller (the evolver store keeps the list), so none drifts back
into the mining set next week.

The two sets are different types, and every miner asserts it was handed a
`MiningSet` -- the held-out sessions are hidden from the proposer by
construction, not by convention, so the gate cannot be overfitted by the thing
it is grading.
"""

import hashlib

HOLD_OUT_MODULUS = 4


class MiningSet(list):
    """Episodes a miner may read."""


class HeldOut(list):
    """Episodes only the gate may replay."""


def is_held_out(session_id):
    digest = hashlib.sha1(session_id.encode("utf-8")).hexdigest()
    return int(digest, 16) % HOLD_OUT_MODULUS == 0


def split(episodes, pinned=()):
    pinned = set(pinned)
    held = [episode for episode in episodes
            if is_held_out(episode.id) or episode.id in pinned]
    if not held and len(episodes) >= 3:
        # Too few sessions for the hash to have picked one: hold out the newest,
        # so there is always something the proposer has not seen.
        held = [episodes[-1]]
    held_ids = {episode.id for episode in held}
    return (MiningSet(e for e in episodes if e.id not in held_ids),
            HeldOut(e for e in episodes if e.id in held_ids))


def require_mining_set(episodes):
    if not isinstance(episodes, MiningSet):
        raise TypeError("miners only read the mining set; held-out sessions are for the gate")
    return episodes
