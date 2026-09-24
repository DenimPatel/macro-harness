# Plan: an offline evolver for macro-harness

*Companion to [`evolving-harness-review.md`](./evolving-harness-review.md) §4. Status: **built**. The README's
"The evolver" section is the user-facing description. See "As built" at the end for where the build
departs from this plan.*

## Context

The review (`docs/evolving-harness-review.md` §4) recommended splitting the
**evolver** from the **harness**. The harness does the work and records
traces. A separate offline process reads those traces, proposes changes,
tests them against a replay set, and puts the survivors in an inbox for the
user to review in a batch. The user wants that integrated with macro-harness.

Decisions already made:
- **In-loop `define_tool` / `define_hook` stay as they are.** The evolver is
  purely additive. It observes those tools in the traces and can propose
  retiring what they created, but it doesn't change them.
- **Sibling package in the same repo:** `evolver/` next to `macroharness/`,
  with its own entry point `mh-evolve`.
- **Two-tier replay gate:** tier 1 replays recorded runs deterministically
  at no cost; tier 2 is an opt-in live replay against the real model, with a
  token budget.

Repo conventions to keep: stdlib only, one module per concern, small enough
to read in a sitting, `unittest` + `tests/fake_model.py`, JSON/JSONL files a
human can read and diff.

## How it works end to end

```
mh (harness)                                   mh-evolve (evolver, offline)
────────────                                   ───────────────────────────
session JSONL  + header (harness digest,  ──►  ingest   → episodes + signals
               git HEAD, model)                 split    → mining set | held-out replay set
               + artifact_use events            mine     → proposals (rule-based miners)
                                                verify   → static checks (reuses harness validators)
loads approved artifacts:                       gate     → tier 1 recorded replay [+ tier 2 live]
  policy.json, hooks.json, tools/*.json,        inbox    → proposals that passed, with contract + evidence
  strategy.json (new, clamped)          ◄──     review   → user accepts/rejects in a batch
                                                apply    → write artifact, mark trusted, ledger entry
                                                stats    → activation/expiry → "retire" proposals
```

The harness never imports the evolver. The evolver touches harness
internals **only** through `evolver/adapters/macroharness.py`, which is the
portability seam. Another harness would get its own adapter.

## Part A: harness changes (small, in `macroharness/`)

1. **Session header + richer trace events**. Changes go in `session.py`
   and `loop.py`; the existing `Session.append` is reused.
   - At session creation, add a `{"t":"header", "harness_digest", "artifacts": {id: sha256},
     "model", "base_url_host", "git_head", "version"}` event. Hash the
     artifacts with `permissions.policy_digest`, which already exists.
     `messages_from_events` ignores unknown kinds, so replay and resume
     aren't affected (confirm with a test).
   - Add `arg` (the primary argument) and an `outcome` field
     (`ok|error|denied|blocked`) to the existing `t:"tool"` event in
     `Harness._run_tool_calls`.
   - Add a `{"t":"turn_end", steps, tokens, stopped}` event at the end of
     `run_user_turn`, where `stopped` is `none|max_steps|budget`.
   - Add a `{"t":"retry", status, attempt}` event. `model.with_retries`
     already takes an `out` callback, so pass one that logs.
2. **`contract.py` (new, ~80 lines).** This is the only harness module the
   evolver adapter depends on for data shapes. It defines:
   - the trace event names and fields;
   - the artifact kinds (`policy_rule`, `hook`, `tool`, `strategy`, `fact`);
   - the ledger entry shape and `digest()`.
3. **Ledger-aware loading + activation events.** The evolver's ledger
   (Part B) records the digest of each artifact it applied.
   - At startup, `__main__.build` reads the ledger's `id → digest` index.
   - When a policy rule, hook, or extension tool that came from the ledger
     is matched or called, the loop logs `{"t":"artifact_use","id":…}`.
     Hook points: `Permissions.authorize` already returns the matched
     `rule`; `Hooks.matching` and `registry.get` run once per call.
   - No behavior changes. This is logging only.
4. **`strategy.py` (new, ~90 lines).** Control-flow settings read from
   `.macroharness/strategy.json`, clamped by constants in code, with trust
   gated like `hooks.json`.
   - Settings: `retry.max`, `retry.backoff_cap`, `retry.on_status`,
     `max_steps`, `parallel_tools`, `compact_at_ratio`.
   - Each value is applied as `min(value, HARD_CAP)`, plus a combined
     budget check on `retry.max × max_steps`.
   - Wire into `Model(max_retries=…)`, `Harness(max_steps=…)`,
     `loop.MAX_PARALLEL_TOOLS`, and `context.COMPACT_AT`. CLI flags still
     win.
   - `base_url` and `api_key_env` are deliberately not settable here.
5. **No other harness behavior changes.** `define_tool` and `define_hook`
   keep working exactly as today.

## Part B: the evolver package (`evolver/`, stdlib only)

| Module | ~Lines | Responsibility |
|---|---|---|
| `__main__.py` | 150 | `mh-evolve ingest \| mine \| gate \| inbox \| review \| apply \| revert \| stats \| replayset` |
| `store.py` | 80 | Evolver state **outside the workspace** at `~/.macroharness-evolver/<sha(workspace)>/` (`--state` overrides): `episodes/`, `replayset/`, `inbox/`, `archive/`, `ledger.jsonl`. Keeping it outside puts the replay set and ledger beyond the reach of the in-loop agent's file tools. |
| `traces.py` | 150 | Read session JSONLs **read-only** via `session.Session.events()` and build `Episode`s (turns, tool calls, outcomes, header). |
| `signals.py` | 120 | Per-turn labels. **Correction**: the next user turn matches `^(no\b\|actually\|that's wrong\|undo\|revert)`. **Restatement**: a near-duplicate user text across sessions, via `difflib.SequenceMatcher` > 0.85. Also repeated asks per `(tool, derived rule)`, repeated tool-error signatures, `max_steps`/`budget` stops, and retries. |
| `split.py` | 40 | Deterministic hash split of episodes into a mining set and a held-out set. Miners only ever receive the mining set; this is enforced by the function signatures. |
| `proposal.py` | 90 | `Proposal`: id, kind, artifact payload, **contract** (`predicts_fix`: episode refs; `risks`; `check`; `expires_if`), provenance (miner, source sessions, model, `tainted` flag if the source turns contained non-user content such as MCP output or web fetches via run_bash), and status. |
| `miners/approvals.py` | 80 | The same ask approved ≥ N times across ≥ 2 sessions → a `policy_rule` proposal built with `permissions.derive_rule`. Never proposes a rule for `define_*` or an `arg:"*"` allow for `run_bash`. |
| `miners/failures.py` | 90 | A recurring tool-error signature (e.g. a `py_compile`-style syntax error after `write_file *.py`) → a `post_tool` capture-hook proposal from a **fixed template catalogue**, not free-form shell. |
| `miners/strategy.py` | 70 | Frequent `max_steps` stops, retry exhaustion on specific statuses, or compaction thrash → a `strategy` setting proposal. |
| `miners/restatement.py` | 60 | Repeated instructions → a `fact` proposal. Stored now; used once `memory.py` exists (listed but not applied until then). |
| `miners/retire.py` | 50 | Ledger artifacts, or in-loop-defined tools and hooks, with 0 `artifact_use` in N sessions, or used under a different model than the one they were learned with → a `retire` proposal. |
| `verify.py` | 120 | Static checks that **reuse harness validators**: `extensions.validate`, `hooks.validate`, `permissions.load_policy` shape, `strategy` clamps. Plus evolver rules: no writes to trust or ledger files; no standing grants (`define_*` allow, `run_bash` `*`); a hook's `{arg}` is only allowed as a quoted `"$MH_ARG"`; no duplicate of an archived rejection. |
| `replay.py` | 200 | **Tier 1 (recorded)**: re-run each episode's recorded assistant messages through a `Harness` built with a `ScriptedModel` (same shape as `tests/fake_model.py`, promoted into `evolver/scripted.py`), in a scratch `git worktree` at the header's `git_head` (or a temp copy if not a git repo), `interactive=False`, once with the baseline artifacts and once with the candidate's. Compare per-call outcome vectors: new denials, new blocks, new errors, and asks removed. **Tier 2 (live, `--live --budget N`)**: run the held-out case's first user prompt against the real `Model` in a scratch worktree, baseline vs candidate, k runs each, scored by the case's check command (exit code), steps, and tokens. |
| `gate.py` | 80 | Accept only if: static checks pass; tier 1 shows **no new errors or blocks on the held-out set** and at least one `predicts_fix` case changes as predicted; tier 2 (when run) shows the held-out pass rate is not lower and tokens are within +10%. The results are stored on the proposal. |
| `inbox.py` | 120 | Renders each proposal as plain-language **effect** ("runs `python3 -m py_compile` after every `*.py` write; output is shown to the model"), the contract, the evidence (episode excerpts), gate results, the file diff, and the taint flag. `review` is a batched y/n/skip prompt; `--accept-all-untainted` is for testing only. Rejected proposals go to `archive/` as negative examples. |
| `apply.py` | 100 | Via the adapter: write the artifact into `policy.json` / `hooks.json` / `tools/<name>.json` / `strategy.json`, `permissions.mark_trusted(...)` with the right key (the review counts as the human approval), append a ledger entry (`id, kind, target, digest, model, applied_at, expires_if, provenance`). `revert <id>` restores the previous file content from the ledger. |
| `stats.py` | 60 | Per artifact: activations, last used, sessions since last use. Per window: correction rate, restatement rate, asks per session, and tokens per completed turn. Printed by `mh-evolve stats`. |
| `adapters/macroharness.py` | 120 | The only module that imports `macroharness.*`: file locations, validators, `Harness` construction for replay, and artifact read/write. |

**Replay set:** `mh-evolve replayset add <session>:<turn> --check "python3 -m unittest -q"`
promotes a held-out turn to a named case with an executable check. Without
a check, tier 2 falls back to "no correction/revert in the recorded
follow-up". That fallback is weaker, and the inbox labels it as weaker.

**Scheduling:** it's manual (`mh-evolve run` = ingest → mine → gate →
inbox). An optional `session_end` hook can call `mh-evolve ingest` using
the existing hook mechanism, so no loop changes are needed. The harness
banner prints `N proposals waiting (mh-evolve review)` when the inbox isn't
empty. It reads a count file the evolver writes; the harness does not
import the evolver.

## Phases (each shippable on its own)

1. **Traces**: Part A.1–A.2 + `evolver/traces.py`, `signals.py`, `stats.py`, CLI `ingest`/`stats`.
   This alone yields the baseline metrics.
2. **Pipeline skeleton**: `store`, `split`, `proposal`, `verify`, `inbox`, `apply`, `revert`, ledger;
   Part A.3 activation events; `miners/approvals.py` as the first miner.
3. **Tier 1 gate**: `scripted.py`, `replay.py` (recorded), `gate.py`, `replayset` command.
4. **Strategy**: Part A.4 + `miners/strategy.py`.
5. **More miners**: `failures.py` (hook templates), `retire.py`, `restatement.py`.
6. **Tier 2 live replay** behind `--live --budget`.
7. *(later, not in this plan)*: an LLM proposer that reads distilled traces in a clean context,
   plus `memory.py` to consume `fact` artifacts.

## Files

- Modified: `macroharness/session.py`, `macroharness/loop.py`, `macroharness/model.py` (retry event
  callback), `macroharness/__main__.py` (header, ledger index, strategy wiring, inbox banner),
  `pyproject.toml` (add `evolver` package + `mh-evolve` script), `README.md` (a new "The evolver" section).
- New: `macroharness/contract.py`, `macroharness/strategy.py`, `evolver/**` as tabulated.
- Reused as-is: `permissions.derive_rule / mark_trusted / trusted_digests / policy_digest`,
  `extensions.validate`, `hooks.validate`, `session.Session.events`, `tests/fake_model.py` pattern.
- Tests: `tests/test_strategy.py`, `tests/test_trace_events.py`, `tests/evolver/test_{signals,miners,verify,replay,gate,apply}.py`.

## Verification

- `python3 -m unittest discover -s tests` passes. The existing 169 tests
  must be unchanged, and resume/compaction must replay identically with
  the new event kinds present.
- **End-to-end scripted scenario** (as a test, offline):
  1. Using `fake_model`, generate 4 sessions where the model runs `pytest -q`
     and the scripted prompt approves it each time (`y`).
  2. `mh-evolve run --state <tmp>` → the inbox holds one `policy_rule`
     proposal `run_bash "pytest *"`, with its contract citing those sessions
     and tier 1 showing asks removed and no new errors on the held-out set.
  3. `review --accept-all-untainted` → `apply` writes the rule, updates
     `trusted.json`, and appends to the ledger.
  4. A new harness session makes zero asks for `pytest -q` and logs an
     `artifact_use` event.
  5. `mh-evolve revert <id>` restores the old `policy.json`.
- Negative tests:
  - A proposal allowing `define_hook` is rejected by `verify`.
  - A hook using a raw `{arg}` is rejected.
  - A candidate that newly blocks a held-out call fails the gate.
  - Miners never see held-out episodes (assert on the IDs they receive).
  - A tainted proposal is labeled in the inbox and excluded by `--accept-all-untainted`.
- Strategy: values above the hard caps are clamped, and `base_url` in
  `strategy.json` is ignored with a warning.
- Optional manual: with `DEEPSEEK_API_KEY` set, `mh-evolve gate --live --budget 50000` on one replay case.

## As built: departures from the plan

Found while building, each for a stated reason:

- **Tests live in `tests/test_evolver/`, not `tests/evolver/`.** `unittest discover -s tests`
  imports a `tests/evolver` package as `evolver`, which shadows the real package.
- **Compaction is not mined.** A session that compacts often is a symptom with several opposite
  cures (compact earlier, keep less, summarize harder). A miner that guesses between them is a miner
  that guesses. Only `max_steps` and `retry.max` are mined.
- **Evolved hooks may inform, not block.** `verify` refuses `blocking: true`. Every catalogue hook
  is a `post_tool` check whose output is shown to the model; a hook that can cancel calls is the
  user's decision to write, not a miner's.
- **Held-out sessions are pinned.** With too few sessions for the hash to pick one, the newest is
  held out; the store records every session ever held out (`held_out.json`), so none drifts back into
  the mining set as sessions arrive, and replay cases stay out of the proposer's reach.
- **A new regression kind: `new_failures`.** `run_bash` reports `outcome: ok` for any exit code, so
  a candidate that makes a held-out command start exiting non-zero would not have been an `error`.
  The gate compares `exit N` too.
- **Gate failures are not permanent; human rejections and reverts are.** A proposal the gate failed
  (for example before any held-out session existed) is re-mined once its evidence has grown. One the
  user rejected, or applied and then reverted, is never proposed again.
- **`evolver/pipeline.py`** holds ingest → mine → gate → inbox as plain functions, so the CLI is a
  thin shell and the tests call the same code.
- **The header records the full `base_url`** as well as its host, so tier 2 can reach an endpoint
  with a path (`/v1`).
- **Artifact-use for extension tools is logged in the serial post-tool loop**, not in `_invoke`,
  which may run on a pool thread for parallel read-only calls.
