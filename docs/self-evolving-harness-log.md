# The self-evolving harness: what we built and why

*A record of the design conversation, the implementation, and the open question
it ended on. Companion to [`evolving-harness.md`](./evolving-harness.md), which
is the research survey this work is grounded in — read that one first for the
prior art (Darwin Gödel Machine, Voyager, ACE, ADAS, SICA, and the pre-LLM
precedents: Emacs, the shell, adaptive query optimizers, MAPE-K). This document
is the narrower, later half: what got built, how it works, and where the
reasoning went next.*

---

## 1. The starting question

> LLMs are general-purpose problem solvers. The harness around them is
> designed and fixed before the session starts. It can't evolve. Example
> capabilities that don't exist yet: retry-if-model-fails, route to two
> different LLMs and verify, sound notifications on completion. Over time the
> harness should become personalized, expert and efficient — without a human
> rewriting it by hand each time.

The concrete target codebase is **macro-harness**: a from-scratch,
stdlib-only agent loop (`loop.py`) plus twelve layers micro-harness leaves out
— retries, streaming, compaction, a permission policy, containment, session
persistence, subagents, MCP, parallel dispatch, token accounting, structured
editing. ~1,400 lines before this work, all fixed at authoring time.

## 2. The key observation the whole design hangs off

**macro-harness already contains a working instance of harness evolution**,
and it shipped before this conversation started. `permissions.py`'s
`always allow` answer:

1. **derives** the narrowest rule that covers the approved call (`src/main.py`
   → `src/*.py`; refuses to widen across shell metacharacters like `;`, `|`,
   `` ` ``);
2. **prints it before writing it** — "silent trust is the thing to avoid, not
   trust itself";
3. **persists** it to `policy.json`, a file a human can read, diff and
   commit;
4. **takes effect immediately**, no restart.

That five-step shape — *propose → show → persist → take effect → account
for* — is the entire mechanism. It's scoped to one narrow decision (which tool
calls need a human), but the shape is right. The design question was never
"how do we invent harness evolution," it was "how far does this one pipeline
generalize."

## 3. Two axes for organizing "what evolves"

### 3.1 Blast radius: what gets written

| Rung | Mutable surface | Example | Reversible by |
|---|---|---|---|
| 0 | Facts | "tests run with `just test`, not pytest" | deleting a line |
| 1 | Policy | `run_bash "pytest *"` → allow | editing a JSON file |
| 2 | Procedures | a saved "release checklist" the agent replays | deleting a file |
| 3 | Tools | a `notify` tool that plays a sound | unregistering it |
| 4 | Control flow | retry classification, two-model verify, dispatch order | `git revert` |
| 5 | Harness source | the agent edits `loop.py` | `git revert`, if you notice |

Most of the *value* is at rungs 0–3. Almost all of the *danger* is at 4–5.
The research literature (DGM, ADAS, SICA) is fascinated by rung 5 because it's
theoretically interesting; the product is mostly at 0–3.

The rung boundary that actually matters: **rungs 0–3 are data, rungs 4–5 are
code.** That decides whether "revert" is a file delete or a source-control
operation, and whether a bad mutation is a wrong answer or a broken harness.

### 3.2 Pressure source: where does the mutation come from

- **Told** — the user states it once ("ping me when you're done"). No search,
  no fitness function. The harness's job is to durably encode the statement
  and never need to be told again. Where personalization actually lives.
- **Observed** — latent in the session log (the same approval ninety times,
  the same failing command). The harness mines it. `always allow` generalized.
- **Searched** — generate variants, score against a task set, keep winners.
  Needs a cheap automatic evaluator, which personal work mostly doesn't have.
  This is the entire self-improving-agent research literature, and the least
  applicable rung to a personal harness.

The three original motivating examples split cleanly: sound-on-completion is
**Told**, retry-on-failure is **Observed**, two-model-verify is **Searched**
(whether it's worth 2x the tokens is empirical). Build in that order.

---

## 4. What we built: rung 3, both pressure sources

We implemented two new modules, both rung 3 (tools), both usable at startup
**and** mid-session — which was the explicit requirement: *"these could be
part of the harness at start or added later during operation."*

### 4.1 `extensions.py` (241 lines) — tools as data

A tool becomes a JSON file: a schema plus a shell command template.

```json
.macroharness/tools/run_tests.json
{
  "name": "run_tests",
  "description": "Run the project test suite.",
  "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
  "command": "python3 -m unittest discover -s {path}",
  "read_only": true
}
```

Loaded at startup by `extensions.load_all`, or written mid-turn by a new
`define_tool` tool the model can call. Both paths end at the same call:
`registry.register(Tool(...))` — the identical call `tools.default_registry`
makes for `read_file`, `write_file`, `edit_file`, `run_bash`. By the time
`loop.py` dispatches a call, **it cannot tell a built-in from a tool invented
four steps ago.** That indistinguishability is the design goal, not an
accident: two registration paths would make the second-class one rot.

This works at runtime because of one existing fact about the loop:
`self.model.complete(self.messages, self.registry.schemas(), ...)` asks the
registry for schemas **on every step**, not once at startup. A tool
registered after step 1 is in the request for step 2.

Three deliberate restrictions, each with a concrete failure mode behind it:

- **A command template, not Python.** Python would run inside the harness
  process, outside `Containment`, and could edit the very registry meant to
  constrain it. A shell command goes through the same `tools.run_command`
  every built-in uses, so it inherits the workspace jail, the timeout, and the
  scrubbed environment.
- **Regex + `shlex.quote` substitution, not `str.format`.** `str.format`
  resolves attributes and indices, so a model-supplied `{path.__class__}`
  would be a sentence in a language nobody meant to accept. Every
  `{placeholder}` must be a declared JSON Schema parameter, or the definition
  is refused at validation time.
- **No shadowing.** A definition may not register a name already in the
  registry. If it could redefine `read_file`, every containment guarantee
  stated in terms of `read_file` would become a guess rather than a fact.

### 4.2 `hooks.py` (333 lines) — pre/post lifecycle hooks

A tool is something the *model* decides to call. A hook is something the
*harness* runs whether the model likes it or not, at a point the model can't
see — where the non-negotiable rules go.

```json
.macroharness/hooks.json
{
  "version": 1,
  "hooks": [
    {"event": "pre_tool", "tool": "write_file", "arg": "*",
     "run": "git check-ignore -q {arg} && exit 1 || exit 0", "blocking": true},
    {"event": "post_tool", "tool": "write_file", "arg": "*.py",
     "run": "python3 -m py_compile {arg}", "capture": true},
    {"event": "turn_end", "run": "afplay /System/Library/Sounds/Glass.aiff"}
  ]
}
```

Five events: `pre_tool`, `post_tool`, `tool_error`, `turn_end`, `session_end`.
`tool` and `arg` are matched with the **same globs and the same
`permissions.rule_matches`** the policy file uses — one matching language in
the harness, not two.

Two of the five events can change what the model sees, which is what makes
hooks more than notifications:

- **`pre_tool` + `blocking: true`** — a non-zero exit **cancels the call**,
  and the hook's output becomes the tool result. The model reads the refusal
  and adapts, exactly as it would read any other tool failure.
- **`post_tool` + `capture: true`** — stdout is **appended to the tool
  result**. A linter that runs after every write feeds its own errors back
  into the conversation without the model having thought to ask. This is the
  harness teaching the model something *inside a single turn*.

Wiring into `loop.py` preserved the two invariants the whole codebase is built
around:

1. **The assistant message is appended verbatim.**
2. **Every tool call gets exactly one result, in call order** — even when one
   of them was blocked. A blocked call still produces one `role: "tool"`
   message; it just contains the hook's refusal instead of the tool's output.

Hooks run **after** the policy, so a hook can only ever *narrow* what
permissions already allowed — a hook cannot resurrect a call the policy
denied. And hooks run serially, in call order, for the same reason
authorization does: they have side effects, and a blocking hook is a decision
the *next* call in the batch may depend on.

`define_hook` is the runtime half — the same pattern as `define_tool`.

### 4.3 Trust, extended rather than duplicated

Both new files live in the workspace, so a cloned repository could ship a
tool definition or a hook that this harness would then execute — the same
problem `policy.json` already had. Rather than invent a second trust
mechanism, `trusted.json` was generalized from a single `policy_sha256` field
to **one digest per file**, keyed by what it's trusting (`tool:run_tests`,
`hooks_sha256`, `policy_sha256`).

**Editing a hook file revokes its trust** — this is the point, not an
annoyance. The digest that was accepted is the only version allowed to run.
Anything written *through* `define_tool` or `define_hook` already crossed the
permission prompt on the way in and is marked trusted at that moment, so it
doesn't ask twice — approval happens once, at the point of creation, not
again at every load.

A `--no-evolve` flag leaves `define_tool` and `define_hook` unregistered, for
a session that should not be able to extend itself at all.

### 4.4 Shared plumbing: `run_command`

`tools.py` gained `run_command(containment, command, extra_env=None,
stdin_text=None, timeout=None)` as the single place a subprocess is started.
`run_bash`, hook execution, and extension-tool execution all now go through
it, so all three inherit the identical jail — same cwd, same scrubbed
environment, same timeout-and-killed-process-group behavior — rather than
three slightly different implementations drifting apart over time.

### 4.5 Verification

169 tests total (115 before this work), including:

- the mid-turn claim itself: a tool defined in the model's first tool call is
  **absent from the schema list sent with the first request** and **present
  in the schema list sent with the second** — proving the capability exists
  because the registry is asked fresh every step, not because of any special
  case;
- a blocking hook cancels a call and the model reads the refusal;
- a batch of calls where one is blocked still produces exactly one result per
  call, in order (the loop invariants survive under hooks);
- a hook cannot resurrect a policy-denied call (hooks run after, and can only
  narrow, never widen);
- editing a hook file on disk silently drops it back to untrusted;
- shell-metacharacter injection through a `{placeholder}` is neutralized
  (`shlex.quote`), and `str.format`-style attribute access is *not* honored;
- a definition cannot shadow `read_file` or any existing tool name.

Plus a live smoke test outside the test suite: agent defines a tool, defines a
`turn_end` hook, calls its own new tool, all inside one turn — then a second,
independent session picks up both from disk with zero re-prompting, confirmed
by inspecting `trusted.json` and `.macroharness/tools/`.

---

## 5. The follow-up question: can rung 4 (control flow) be modified at runtime too?

This was your direct follow-up, and it sharpens rather than contradicts the
table in §3.1.

### 5.1 The rung number is a property of representation, not of the decision

The ladder isn't "facts are inherently safer than control flow." It's **"data
is safer than code."** Retry classification sits at rung 4 only because it is
currently spelled as an `if` statement in `model.py`. Move the same decision
into `strategy.json` and read it at runtime, and it is now rung 1 — narrow,
declarative, revertible by editing a file.

So the honest answer to "can rung 4 be modified at runtime" is: **you don't
modify rung 4 at runtime — you demote it to rung 1 first, and then it's
modifiable by construction.** This is exactly what happened to the
permission decision: "which calls need a human" used to be a hardcoded `y/n`
branch in micro-harness. It became data, which is the only reason
`always allow` can exist at all.

### 5.2 Only one kind of control flow demotes cleanly

**Parameters of a control-flow shape that already exists** demote for free:
retry count, backoff base, which HTTP status codes are retryable,
`MAX_PARALLEL_TOOLS`, `max_steps`, `COMPACT_AT`, which model serves which
request. The code keeps one shape and reads its constants from a file. This
is most of what anyone actually wants to tune, and it's nearly free to build.

**New control-flow shapes** don't. Two-model verify, generate-N-and-rank, a
debate loop, reflect-before-answer — these are new graph topologies, not
parameters. JSON can express them only through an *interpreter* for a small
language of shapes:

```json
{"step": "verify_write",
 "plan": [{"call": "model_a"}, {"call": "model_b", "role": "critic"},
          {"gate": "both_agree", "else": "ask_user"}]}
```

And the honest cost: this doesn't eliminate rung 5, it **relocates** it. The
set of expressible behaviors is bounded by the interpreter's vocabulary —
`{"gate": "both_agree"}` only works because someone wrote `both_agree` in
Python. A genuinely novel primitive still needs a source edit. Still a good
trade (bounded rung-1 config plus occasional rung-5 vocabulary extension,
instead of unbounded rung-5 editing), but a trade, not a free lunch — and the
finiteness of the vocabulary is exactly what makes it safe.

### 5.3 The named failure mode, and the counter-precedent that solves it

There's a well-known way "let's make the control flow data-driven" goes
wrong: the config grows an `if`, then a loop, then variables, and the result
is a bad programming language with no debugger, no stack traces, no type
checker. This has a name — the **configuration complexity clock**
(hardcoded → config → DSL → rules engine → "we should have just written
code") — and it's the same observation **Greenspun's Tenth Rule** makes from
the opposite direction.

The precedent that actually solves it safely is **eBPF**: loading new control
flow into a *running Linux kernel*, arguably the most safety-critical program
on the machine, made safe not by a weak language but by three properties:

1. A **restricted bytecode** — originally not Turing-complete, no unbounded
   loops.
2. A **static verifier** that proves termination and memory safety *before*
   load, with rejection as the default outcome.
3. **Only then**, JIT to native.

The transferable lesson: **safe rung-4 mutation needs a verifier, not just a
schema.** `hooks.json` gets away with a plain validator because a hook is a
leaf — it can't loop the harness back on itself. A strategy graph *can*, so it
needs a real check: acyclic, every node name in the known vocabulary, bounded
worst-case model-call count.

The older, non-AI statement of the same idea is **policy/mechanism
separation** (Hydra, Mach): mechanism stays in code, policy becomes data. The
production-scale proof this works is the **adaptive query optimizer**
(System R → DB2 LEO → modern cost-model feedback) — the query *plan* is data
and freely mutable at runtime; the *executor* that runs each plan node is
fixed and is not.

### 5.4 The safety property that does not survive the demotion for free

This is the genuine gap in the original blast-radius table, surfaced only by
asking the follow-up question.

Demoting a decision to JSON preserves reversibility, diffability, and
no-restart. It does **not** automatically preserve *bounds*. `max_steps=16`
as a Python literal was a value a human chose once. `{"max_steps": 100000,
"parallel": 500}` in a config file is now something a mutation — mined,
proposed, or model-written — can produce, and the harness will happily try to
honor it and burn the entire token budget doing so. The config can express
states the original hardcoded version never could.

So the containment rule that already exists in `tools.py` has to carry over
exactly, not just in spirit: *containment runs after the policy, so no rule
can authorize leaving the workspace.* The strategy-config equivalent is that
the interpreter **clamps**: `max_steps` is read from config but applied as
`min(config_value, HARD_CAP)` where `HARD_CAP` is a Python constant, not
itself configurable. Data proposes; code bounds.

And within "rung 1" there turn out to be sub-tiers by blast radius, which the
original table collapsed:

| Config field | Real risk if mutated |
|---|---|
| `retry.max`, `backoff.base`, parallel width | token budget only |
| `max_steps`, `compact_at` | budget, context correctness |
| **`model.base_url`, `api_key_env`** | **exfiltration — a trust boundary, not a knob** |

A strategy file that can name a network endpoint is categorically different
from one that names a retry count. If `strategy.py` gets built, routing-by-
endpoint should be the one field that is **not** agent-writable — the same
reasoning that already applies to "the mutation writer must not be able to
edit the trust anchor or the containment code."

### 5.5 Where this leaves the two original motivating examples

- **Adaptive retry** — pure parameters. `strategy.json` with something like
  `{"retry": {"max": 3, "on_status": [429, 500, 502, 503], "backoff": 2,
  "jitter": true}}`, read by `model.py`. Fully runtime-mutable, clamped in
  code, cheap to build — the ~80% case.
- **Two-model verify** — a new shape. Two honest options: a hardcoded
  `{"verify_writes": true}` boolean over a fixed Python implementation
  (trivial, inflexible, probably correct as a first cut), or a real
  mini-interpreter for graph shapes (powerful, and exactly where the
  complexity clock starts ticking). Recommendation reached in discussion: do
  the boolean flag first, and only build the interpreter if a third and
  fourth shape turn out to be genuinely wanted — the clock only bites if you
  start it early.

**Not yet built.** `strategy.py` is scoped and reasoned through but not
implemented in this repository as of this document. It's the natural next
rung after the two shipped here.

---

## 6. Summary table: state of the six-stage plan

From `evolving-harness.md` §7, updated with what's now built:

| Stage | Module | Rung | Status |
|---|---|---|---|
| 1 | `memory.py` | 0 (facts) | not built |
| 2 | `mine.py` | 0–1 (mined facts/policy) | not built |
| 3 | `hooks.py` | 3 (tools, lifecycle) | **built** — 333 lines, 5 events, pre/post can gate or augment |
| 4 | `extensions.py` | 3 (tools, capability) | **built** — 241 lines, runtime-registered, mid-turn usable |
| 5 | `strategy.py` | 4 demoted to 1 (control-flow parameters) | scoped, not built — clamped config + verifier needed for shapes beyond parameters |
| 6 | archive / auto-PR | 5 (source) | design-only; recommendation is offline, human-reviewed, never in the live loop |

Stages 3 and 4 were taken first, out of order relative to the original
value-per-risk ranking, because between them they answer "add a capability"
and "run something every time" — most of what a user actually asks a harness
for — and because both are rung 3, where a mistake is still a deleted file.

---

## 7. Repository state

- Branch: `claude/llm-harness-evolution-dl91vy`
- `macroharness/extensions.py` — 241 lines
- `macroharness/hooks.py` — 333 lines
- `macroharness/loop.py` — grew to 309 lines (hook call sites at both
  invariant-preserving points)
- `macroharness/permissions.py` — grew to 277 lines (`trusted_digests`,
  multi-key `mark_trusted`)
- `macroharness/tools.py` — grew to 237 lines (`run_command` extracted as
  shared subprocess plumbing)
- `macroharness/__main__.py` — wires `Hooks.load`, `extensions.load_all`,
  registers `define_tool`/`define_hook` unless `--no-evolve`; adds `/tools`
  and `/hooks` REPL commands
- `tests/test_extensions.py`, `tests/test_hooks.py` — new
- `tests/fake_model.py` — extended with hook support for the test harness
  factory
- `docs/evolving-harness.md` — the research survey (written first)
- `docs/self-evolving-harness-log.md` — this document
- 169 tests, all passing
