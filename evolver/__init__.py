"""The offline evolver for macro-harness.

The harness does the work and leaves a trace. This package, run later and
separately as `mh-evolve`, reads those traces, proposes changes, replays
recorded sessions to test them, and queues what survives for a human to review
in one batch. It never runs inside a turn, and the agent doing the work never
writes durable harness state through it.

Only `adapters/macroharness.py` imports the harness.
"""
