# The self-evolving harness: a review of the idea

*A design review of the proposal in
[`self-evolving-harness-log.md`](./self-evolving-harness-log.md) (and the survey
[`evolving-harness.md`](./evolving-harness.md)). It covers **the idea as
something to build next**, not any existing codebase. It draws on the
2025–26 research on harness self-evolution (sources at the end).*

---

## 0. The idea, as I understand it

> The harness around an LLM is fixed before the session starts. It should
> instead become personalized, expert and efficient over time — gaining
> capabilities like retry-on-failure, two-model verification, or a sound on
> completion — without a human rewriting it by hand.

Main design moves in the proposal:

- A **ladder of mutable surfaces** ordered by blast radius: facts → policy →
  procedures → tools → control flow → harness source.
- **Three pressure sources**: *Told* (user states it), *Observed* (mined from
  logs), *Searched* (generate variants, score, keep winners).
- **One pipeline** for every change: propose → show → persist → take effect →
  account for.
- **Demote code to data** ("data proposes, code bounds"). New control-flow
  shapes need a verifier, the eBPF lesson, not just a schema.
- Build order **Told → Observed → Searched**. Rung 5 stays offline.

## 1. Verdict

The instincts are good. Staying away from source rewriting, preferring data
to code, clamping config in code, and "a verifier, not a schema" all match
what the research has since found. The weak spot is that **the proposal is
almost entirely about how changes get made, and says almost nothing about
how changes get judged.** The field's clearest lesson from 2025–26 is the
reverse:

> "The gate, not the proposer, does the work." Across SkillOpt, AHE, DGM
> and Self-Harness, generating candidate edits is easy. Deciding which ones
> actually help is the binding constraint. SkillOpt accepts 1–4 edits out
> of large rejection buffers. AHE is ~5× better at predicting what an edit
> fixes than what it breaks.

Without a gate you get a harness that *changes* over time, not one that
*improves*. Changes that nobody checks tend to make things worse, because
additions cost context, conflict with each other, and go stale.

Five changes would make the design much stronger:

1. **Design the gate first** (§2).
2. **Measure whether changes are used and followed, not only whether they
   exist** (§3).
3. **Split the evolver from the harness** (§4).
4. **Replace the data-vs-code axis with effect × author × persistence**
   (§5).
5. **Plan for misevolution and injection that persists, and treat them as
   core requirements** (§6).

---

## 2. The main gap: nothing judges whether a change helped

### 2.1 Why "the user didn't object" isn't enough

For the Told and Observed tiers, the proposal's fitness signal is
effectively "the user approved it and it's reversible". That tests whether
a change is *acceptable*, not whether it *helps*. Some predictable
failures:

- A retry policy that hides a real bug by retrying until it goes away.
- A `post_tool` linter hook that adds noise to every turn and makes the
  model worse on unrelated work.
- A mined "always allow" that turns an approval given out of fatigue into a
  permanent rule.

Each would be approved, each is reversible, and each can quietly lower
quality. Reversibility only helps if someone notices there's a problem to
revert.

### 2.2 A personal harness has more of a gate than you'd think

The survey says "personal work has no automatic evaluator" and so rules
out Searched. That's too pessimistic. Your own session history can supply
tasks and labels:

| Signal | Where it comes from | What it tells you |
|---|---|---|
| **Correction rate** | user says "no, …", "actually …", re-asks the same thing | the turn failed |
| **Undo / revert rate** | git reverts, file rollbacks, `/undo` shortly after a turn | the output was wrong |
| **Restatement rate** | same instruction appears across sessions | a Told fact wasn't stored |
| **Approval / deny rate** | permission prompts | the policy is too tight or too loose |
| **Executable checks** | tests, type checker, build, linter exit codes | objective pass/fail for coding work |
| **Cost / latency per completed task** | token accounting | efficiency |

From these you can build a **personal replay set**: 20–50 real past tasks
where you know the outcome (tests passed, the user accepted it, no revert).
That's your held-out split. A replay against it is a non-regression check,
just like Self-Harness's "must improve one split without degrading the
other".

### 2.3 Attach a falsifiable contract to every change

A pattern from AHE and HarnessBank that's easy to reuse: every proposed
change states up front **what it should fix and what it might break**:

```yaml
change: retry model calls on 529 with jittered backoff, max 3
predicts_fix: [sessions 0412, 0419 aborted on transient 529]
risk: [slower failure on real auth errors; token cost on long turns]
check: replay the 2 fix cases + 10 random held-out tasks; tokens within +5%
expires_if: zero activations in 30 days
```

This makes each change testable, gives you lineage for free, and lets you
report "this change did what it said". AHE's finding that proposers predict
fixes much better than regressions is exactly why the `risk` line has to be
checked against held-out tasks, not taken on trust.

### 2.4 The small-sample problem is real, so design around it

One user produces few, varied sessions. You won't get statistical power to
A/B small effects. Accept that and:

- Evaluate changes with **targeted replays** (the cases the change says it
  fixes, plus a small regression sample), not population averages.
- Prefer changes whose benefit is **mechanically obvious** (a retry that
  turned an abort into a success) over ones whose benefit is statistical
  (a prompt tweak that "seems better").
- Treat anything whose benefit can only be judged statistically as
  *Searched*, and run it offline in batches, not live.

---

## 3. A change that exists isn't necessarily a change that helps

*"Harness Updating Is Not Harness Benefit"* (2026) splits self-evolution
into two separate abilities:

- **Updating**: writing a useful change. This turns out to be *flat* across
  model sizes; a 9B model writes changes nearly as good as a frontier
  model's.
- **Benefit**: actually using the change. Weak models load skills only ~25%
  of the time (vs ~96% for strong ones), and their adherence decays over
  long trajectories (0.52 → 0.13).

What this means for your design:

1. **Track activation and adherence for every change.** Is the skill
   loaded, is the tool called, is the fact followed in the behavior that
   follows? A change with 0% activation isn't harmless. It costs context
   and gives nothing back.
2. **Prefer changes the harness enforces over changes the model has to
   remember.** A hook that *runs* the linter has 100% adherence by
   construction. A fact saying "remember to run the linter" depends on the
   model. This is a strong argument for rungs 3–4 (tools, hooks, middleware)
   over rung 0 (facts) whenever the behavior can be made mechanical.
3. **Expect a model upgrade to wipe out much of what was learned.**
   Terminal-Bench 2026 found model swaps beat scaffold swaps (+52% vs +17%).
   Mid-tier models gain most from harness evolution, and frontier models
   gain less. Many learned workarounds will become useless, or harmful, on
   the next model. Tag each change with the model it was learned under and
   re-check it on upgrade. Hold the harness loosely: it helps you *discover*
   good behavior, and some of that will later be built into models anyway.

The AHE ablation also found that for coding agents, **prompt-only evolution
regressed** (−2.3 points), and the gains came from tools, middleware and
memory. That challenges the proposal's claim that "most of the value is at
rungs 0–3 and almost all the danger is at 4–5". For coding work, a lot of
value sits in rung-4 middleware (retry, verification, context management).
The right response isn't to avoid rung 4. It's to demote it to
parameters, which the proposal already does, *and* gate it (§2).

---

## 4. Split the evolver from the harness

The proposal pictures the harness evolving *itself*, with the running agent
calling something like `define_tool` in the middle of a turn. I'd make the
split explicit:

```
┌────────────── harness (runtime) ──────────────┐     ┌──────── evolver (offline) ────────┐
│ fixed kernel: loop, sandbox, permission check, │     │ reads traces → proposes changes   │
│ hard caps, trust store, artifact verifier      │     │ → replays → gates → writes        │
│ loads versioned artifacts, emits traces ───────┼────►│   proposals to an inbox           │
│                          ◄─── approved artifacts ──┤  (user reviews in a batch)        │
└────────────────────────────────────────────────┘     └───────────────────────────────────┘
```

Why this is better:

- **Safety.** The agent doing your work never writes durable harness state
  while untrusted content (web pages, READMEs, MCP output) is in its
  context. Evolution happens in a separate process, with a clean context,
  reading traces as *data*.
- **Cost and latency.** You don't pay for evolution in the middle of a
  task. The survey already says mutation belongs at a boundary. This makes
  it structural.
- **A place for the gate.** Replays and non-regression checks need time and
  compute, which fits naturally in an offline process.
- **Portability.** If artifacts use open formats (SKILL.md-style
  procedures, hook configs, MCP servers, a plain `strategy.json`), the
  evolver can target *any* harness, including Claude Code, Codex, or your
  own. That makes **the evolver the product**, and it's a more defensible
  one than yet another agent loop.

What stays in-session is only **Told**, and only in **session scope** by
default ("ping me when done" works right away for this session). Making it
durable is a proposal that goes through the inbox.

---

## 5. Rethink the blast-radius axis

The ladder's organizing claim is *"rungs 0–3 are data, rungs 4–5 are
code"*. The flaw is that storing something as data changes how you
**revert** it, not what it can **do**. A hook stored as JSON that runs a
shell command on every tool call is data in the same sense `~/.bashrc` is
data. The proposal notices this itself with the `base_url` field ("a trust
boundary, not a knob"), but the point applies across the whole ladder.

Classify each change on three independent axes:

| Axis | Low | High |
|---|---|---|
| **Effect**: what it can touch | text shown to the model | shell, network, credentials, the harness's own state |
| **Author**: who produced it | the user, typing it | the model, with untrusted content in context |
| **Persistence**: how long it lives | this turn / session | every future session, auto-loaded |

Treat the level of review as a function of all three: a user-typed,
session-scoped fact needs nothing; a model-authored, persistent,
shell-executing hook needs the full gate and explicit approval. The
existing ladder is still useful as a *catalogue* of surfaces. It just
shouldn't be the *risk* model.

---

## 6. Safety: plan for misevolution

*"Your Agent May Misevolve"* (2025) documents emergent risks in
self-evolving agents built on frontier models, across memory, tool and
workflow evolution. Examples include **safety alignment degrading as
memory accumulates**, and **vulnerabilities introduced when tools are
created and reused**. Your design needs specific answers to these.

### 6.1 Injection that persists is the main threat

A single prompt injection is bad. One that writes itself into the harness
**survives every future session**. Take Simon Willison's "lethal trifecta"
(private data + untrusted content + a way to communicate out) and add a
fourth element: **a way to persist**. Attacks already seen in the wild in
this shape include the persistent-memory injection "SpAIware" against
ChatGPT memory (2024) and MCP tool poisoning / rug-pull attacks (2025).

Mitigations:

- **Taint tracking.** Record what was in context when each change was
  proposed. A change proposed while untrusted content was present can be
  session-scoped only. (§4's split largely does this by construction.)
- **Provenance on every artifact:** which session and turn, which user
  message, what evidence. This is also how you answer "why does my harness
  do this?" six months later.
- **Treat anything a change writes into context as untrusted**, e.g. a
  hook that captures output into the model's context.

### 6.2 The trust anchor and the checks must be outside the evolver's reach

STOP's agent turned off its sandbox flag, and DGM's agent removed the
markers its hallucination checker relied on. Rules that follow:

- The evolver can't write the trust store, the permission evaluator, the
  sandbox, hard caps, or the evaluation/replay harness. Enforce this in
  code or by filesystem permissions, not by convention.
- Keep the **replay set hidden from the proposer**, or it will overfit.
  Adaptive Auto-Harness saw its skill set grow 2 KB → 68 KB and stall.
- Changes the user wrote are **immutable to the evolver**, not merely
  "trusted". Otherwise a model that keeps failing a user's lint hook has
  both a reason and a way to weaken it.

### 6.3 One approval must not cover an unbounded class of changes

The "always allow" pattern that inspired the design is safe only because
it derives a *narrow* rule. The same pattern applied to "may define tools"
or "may define hooks" becomes permission to do anything. Self-extension
should cost one approval per change, never a standing grant.

### 6.4 Approvals given under fatigue are weak evidence

Research on permission dialogs (Android runtime permissions, Windows UAC)
consistently finds that more prompts lead to more approvals and less
scrutiny. That affects two parts of the design:

- **Approval UX.** Show the *effect* in plain words ("runs a shell command
  after every file write; its output will be shown to the model"), a
  dry-run result, and the contract (§2.3), not raw JSON. Batch changes in
  an inbox instead of interrupting mid-task.
- **The Observed tier.** "Approved the same call 90 times" may just mean
  "clicked yes 90 times". Mining should *propose*, with the evidence, and
  never apply on its own.

---

## 7. Accumulation is the default failure mode

Every system that only adds things eventually degrades: `.emacs`
bankruptcy, stale wikis, and for agents specifically, the "curse of
abundance". Tool-selection accuracy drops as more tools are added, and
retrieval over hundreds of skills becomes the bottleneck. Build these in
from day one:

- **Stats on every artifact:** activations, errors, last used, model it was
  learned under.
- **Expiry by default.** Unused for N sessions → deactivated (archived,
  restorable).
- **A context budget.** At most K evolved tools or skills in the active
  prompt. Beyond that, lazy loading or a `find_skill` meta-tool.
- **Conflict detection** when something is written: two facts that
  contradict, two hooks on the same event, a hook that blocks something
  policy allows.
- **Consolidation.** Periodically merge overlapping artifacts, using
  deterministic merges instead of LLM rewrites, to avoid ACE's "context
  collapse" / brevity bias.

---

## 8. Smaller points on specific parts of the proposal

- **Told needs more design than it seems.** Preferences have *scope*
  (this repo, this language, always), *conditions* ("ping me when done"
  only for long tasks, or only when I'm away), and *conflicts* ("be
  terse" vs "explain your reasoning"). A flat list of facts won't hold up.
  Store scope and condition explicitly, and resolve conflicts by recency
  plus scope specificity, like CSS.
- **Two-model verify.** Models trained on similar data make correlated
  errors, and LLM judges favor outputs that resemble their own. Agreement
  is weaker evidence than it looks. For coding, an executable check
  (tests, types, build) usually beats a second opinion per token. If you
  do use a critic, use a different model family and calibrate it on cases
  where you know the answer.
- **Clamps are necessary but not sufficient.** "Data proposes, code
  bounds" handles single values. Also bound *combinations*: retries ×
  parallelism × max steps can multiply spend even when each value is
  within its cap. Put a per-turn token/cost budget in the kernel.
- **Reproducibility.** Once the harness changes between sessions, a
  session log only makes sense alongside the harness state that produced
  it. Write a digest of all active artifacts into every session header.
  Your replay gate (§2.2) depends on this.
- **Watch the configuration complexity clock.** The proposal is right to
  put off a strategy-graph DSL until a third and fourth shape are needed.
  I'd go further: when you need a new shape, write it in code, reviewed
  like any PR, and expose only its *parameters* to evolution.

---

## 9. What I'd build, in order

Each phase is useful on its own. None relies on a later phase to justify
it.

1. **Traces + metrics.** A structured trace per session, the signals in
   §2.2, and a harness-state digest per session. *Without this nothing
   later can be judged.*
2. **Personal replay set.** 20–50 past tasks with known outcomes, hidden
   from the proposer. A single command replays them against any harness
   state.
3. **Told, done properly.** Scoped, conditional preferences.
   Session-scoped right away; durable through the inbox. Measure
   restatement rate.
4. **Artifact lifecycle.** Propose (with contract) → verify statically →
   dry-run → replay gate → inbox → activate in scope → measure activation
   and adherence → expire. Every artifact type goes through this one
   pipeline.
5. **Observed evolver.** Offline miner over traces: repeated approvals,
   repeated failures, repeated manual steps. Output is *proposals*, never
   direct writes.
6. **Parameters of control flow** (retry, backoff, budgets, routing between
   *pre-approved* models), clamped in the kernel and gated by replay.
7. **Searched, offline, narrow.** Only for surfaces with an executable
   verifier (coding tasks with tests). Population/Pareto-style search
   (GEPA-style) over parameters and procedures, with an archive of rejected
   candidates kept as negative feedback.
8. **Harness source changes: never in the loop.** At most, the evolver opens a PR
   against the harness repo for human review.

### How you'll know it worked

- Restatement and correction rates fall, and the revert rate doesn't rise.
- Replay pass rate on held-out tasks doesn't regress. Tokens per completed
  task fall.
- Most active artifacts have high activation. Artifacts expire regularly
  instead of piling up.
- After a model upgrade, the re-check retires some artifacts. If it
  retires none, you aren't measuring.

---

## 10. Checklist

- [ ] Is there a gate that can say *no* to a change based on evidence, not
      just on user approval?
- [ ] Does every change state what it fixes and what it might break?
- [ ] Is the replay/eval set hidden from the proposer?
- [ ] Do you measure activation and adherence, not just existence?
- [ ] Is evolution offline and out of the task loop (except session-scoped
      Told)?
- [ ] Can the evolver write the trust store, sandbox, caps, or evaluator?
      (It must not.)
- [ ] Is untrusted content in context tracked, and does it block
      durable changes?
- [ ] Can one approval authorize an unbounded class of future changes?
- [ ] Is the default scope of a new artifact "this session"?
- [ ] Are combinations of knobs bounded by a cost budget in the kernel?
- [ ] Does anything ever expire? Is there a context budget for artifacts?
- [ ] Does each session record the harness state it ran under?
- [ ] Are artifacts tagged with the model they were learned under, and
      re-checked on upgrade?

---

## Sources

- Zhang, [*Self-Evolving Agentic Harnesses*](https://jxzhangjhu.github.io/blog/2026/self-evolving-agentic-harnesses/) (2026): overview of the field. "The gate, not the proposer, does the work"; AHE, SkillOpt, HarnessX results; Terminal-Bench model-vs-scaffold comparison.
- [*Harness Updating Is Not Harness Benefit*](https://arxiv.org/html/2605.30621v1) (2026): the updating/benefit split; activation and adherence failures.
- [*Self-Harness: Harnesses That Improve Themselves*](https://arxiv.org/html/2606.09498v1) (2026): held-in/held-out non-regression acceptance.
- [*HarnessBank: Gated Verification for Harness Self-Evolution*](https://arxiv.org/pdf/2607.13683) (2026).
- [*Verify Smarter, Evolve Further*](https://arxiv.org/pdf/2608.27311) (2026): behavior-aware verification cost.
- [*SEAGym*](https://arxiv.org/pdf/2606.17546) (2026): evaluation environment for self-evolving agents.
- Shao et al., [*Your Agent May Misevolve*](https://arxiv.org/abs/2509.26354) (2025): emergent risks across memory, tool and workflow evolution.
- [*A Comprehensive Survey of Self-Evolving AI Agents*](https://arxiv.org/pdf/2508.07407) (2025) and [*A Survey of Self-Evolving Agents: What, When, How, Where*](https://arxiv.org/pdf/2507.21046) (2025).
- Zhang et al., *Darwin Gödel Machine* (2025); Hu et al., *ADAS* (2024); Wang et al., *Voyager* (2023); Zhang et al., *ACE* (2025); Agrawal et al., *GEPA* (2025).
- Zheng et al., *Judging LLM-as-a-Judge* (2023): self-preference bias.
- Rehberger, *SpAIware* (2024); Invariant Labs, *MCP tool poisoning* (2025); Willison, *The lethal trifecta* (2025).
