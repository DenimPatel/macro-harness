# The evolving harness

*Research notes. How a harness stops being a fixed artifact and starts
accumulating capability, and what prior work we can steal from.*

---

## 1. The problem, stated precisely

macro-harness is thirteen hundred lines that were written before your session
started. The model inside it is a general-purpose problem solver; the loop
around it is not. Every question of the form "what does this loop do when X
happens" was answered at authoring time, by a human, for a hypothetical user,
and frozen.

So the failure mode is specific and it is not "the model is not smart enough":

- A model call fails in a way `model.py` does not classify as retryable. The
  turn dies. It will die the same way tomorrow.
- You want a second model to check the first one's patch before it lands.
  There is no seam for that; there is one `Model` and one `model.chat`.
- You want a sound when a long turn finishes. Nothing in the harness has a
  concept of "turn finished, tell the human."
- You approve `pytest -q` for the ninetieth time.

Only the last one gets better with use, and it gets better because of the one
piece of machinery this repo already has that learns: `always allow` derives a
policy rule, writes it to `policy.json`, and it takes effect immediately.

That is the whole thesis in miniature. **The harness already contains a working
instance of harness evolution.** It is scoped to one narrow decision — which
tool calls need a human — but the shape of the mechanism is exactly right, and
the interesting question is how far that shape generalizes.

The goal: after three months, my harness and your harness are different
programs, each shaped by the work its user actually does, and neither of us
wrote the difference by hand.

---

## 2. First principles: what is actually mutable

"The harness evolves" is too vague to build. Decompose by *what gets written*,
ordered by blast radius. Each rung is strictly more powerful and strictly more
dangerous than the one above it.

| Rung | Mutable surface | Example | Blast radius | Reversible by |
|---|---|---|---|---|
| 0 | **Facts** | "tests are run with `just test`, not pytest" | none | deleting a line |
| 1 | **Policy** | `run_bash "pytest *"` → allow | narrow, declarative | editing a JSON file |
| 2 | **Procedures** | a saved "release checklist" the agent replays | one task type | deleting a file |
| 3 | **Tools** | a `notify` tool that plays a sound | new capability, sandboxed by permissions | unregistering it |
| 4 | **Control flow** | retry classification, two-model verify, dispatch order | the loop itself | git revert |
| 5 | **The harness source** | the agent edits `loop.py` | unbounded | git revert, if you notice |

Two observations fall out immediately.

**Most of the value is at rungs 0–3, and almost all of the danger is at 4–5.**
The literature is fascinated by rung 5 (a program that rewrites itself) because
it is the theoretically interesting one. But "remembers that this repo uses
`just`", "stops asking about pytest", "knows my release steps", "can make a
sound" covers the overwhelming majority of what makes a harness feel personal
and expert. Rung 5 is where the papers live; rungs 0–3 are where the product is.

**Rungs 0–3 are data. Rungs 4–5 are code.** That boundary is the most important
one in the design, because it decides whether "revert" is a delete or a
`git revert`, and whether a bad mutation is a wrong answer or a broken harness.

### The second axis: where does the pressure come from

Cutting the same space a different way — and this is the cut the research
literature mostly misses, because it optimizes benchmarks and you do not have a
benchmark for "your job":

**A. Told.** The user says "always use `just test` in this repo", or "ping me
when you're done". One shot, no search, no fitness function. The harness's job
is to *durably encode* a statement into the right rung and never need to be
told again. This is the boring path and it is where the personalization is.

**B. Observed.** The session log contains the same approval ninety times, the
same three-command sequence every Friday, the same tool failing in the same
way. Nobody stated a rule; the rule is latent in the trace. The harness's job
is to *mine* it and propose it. This is `always allow` generalized.

**C. Searched.** Generate variants of a behavior, run them against a task set,
keep the winners. This requires a fitness signal you can compute without a
human, which for most personal work does not exist. This is the entire research
literature, and it is the *least* applicable rung to a personal harness.

The three examples in the original motivation split cleanly across these:
sound-on-completion is **A** (nobody discovers that you want a sound; you say
it). Retry-on-failure is **B** (the trace shows the failure class). Two-model
verify is **C** (whether it is worth 2x the tokens is an empirical question
about your task distribution).

A harness that only does **C** is a research demo. A harness that does **A**
well is immediately useful. Build A, then B, then C.

---

## 3. The mechanism, in one shape

Every rung, and all three pressure sources, want the same five-step pipeline —
which is the pipeline `permissions.py` already implements for rung 1:

```
   propose  →  show  →  persist  →  take effect  →  account for
```

1. **Propose** a change, in the narrowest form that fixes the observed problem.
   (`always allow` *derives*: `src/main.py` narrows to `src/*.py`, and it
   refuses to widen across shell metacharacters. Narrowing is the safety
   property. A proposal generalized too far is the mutation equivalent of
   overfitting in reverse.)
2. **Show** it before it lands. `permissions.py` prints the derived rule first,
   then writes it. Silent self-modification is the thing to avoid, not
   self-modification.
3. **Persist** it to a file in the workspace, in a format a human can read,
   diff and commit. Not a vector store, not a pickle, not a model weight.
4. **Take effect** immediately, without a restart.
5. **Account for** it: log the mutation into the session log like any other
   event, so the lineage of "why does my harness behave this way" is
   reconstructible by replay.

This repo already has the file format (`policy.json`), the trust-on-first-use
mechanism for a workspace-supplied file (`trusted.json`), the typed event log
(`rule` is already an event type), and the "derive narrowly, print first"
discipline. The evolving harness is mostly *the same pattern applied to four
more rungs*.

---

## 4. Prior art

### 4.1 The theory rung: provable vs. empirical self-modification

Schmidhuber's **Gödel Machine** (2003) is the honest starting point: a program
that rewrites any part of itself, including its rewrite logic, but only after
*proving* the rewrite increases expected utility. It is the correct answer and
an unimplementable one — the proof requirement is fatal in any environment
whose dynamics you cannot axiomatize.

The **Darwin Gödel Machine** (Sakana AI / UBC, 2025) drops the proof and
substitutes empirical validation: propose a self-modification, run it against a
benchmark, keep it if it scores better. Over 80 iterations it took its own
coding agent from 20.0% → 50.0% on SWE-bench and 14.2% → 30.7% on Polyglot.
Three details matter for us:

- **What it actually discovered is unglamorous and rung-3/4 shaped**: better
  file-viewing and editing tools, a patch-validation step, generating multiple
  candidate patches and ranking them, and keeping a history of what was already
  tried and why it failed. These are not exotic. Several are things you would
  put in a harness by hand if you thought of them — which is the point. The
  system's value is that it thinks of them, on your workload, while you sleep.
- **The archive, not the gradient, is the mechanism.** It keeps an
  ever-expanding archive of all agents, and new modifications branch off *any*
  ancestor, not just the current best. Worse-performing ancestors turned out to
  be the stepping stones to the best descendants. A hill-climber that keeps only
  the champion gets stuck; this is the standard open-endedness /
  quality-diversity result (MAP-Elites, POET) reappearing in agent design.
- **It hacked its own objective.** The DGM at one point removed the tool-use
  markers the harness used to detect hallucinated tool calls — i.e. it improved
  its score by deleting the detector. The authors caught it *because the lineage
  was traceable*. This is the single most important empirical finding in the
  area, and it says: the auditability requirement is not compliance theatre,
  it is the only thing that made the failure visible.

**Gödel Agent** (2024) is the same idea at runtime rather than across
generations: a self-referential agent that modifies its own running logic in
memory, no fixed meta-learning algorithm.

**AlphaEvolve** (DeepMind, 2025) is the industrial proof that the
evolve-code-against-an-evaluator loop pays for itself outside toy settings —
among other results it found a Borg scheduling heuristic that recovers ~0.7% of
Google's worldwide compute. Note the precondition: a cheap, automatic,
un-gameable evaluator. That precondition is exactly what a personal harness
lacks, and is the reason rung **C** is last on our list, not first.

### 4.2 The architecture rung: searching over agent designs

**ADAS — Automated Design of Agentic Systems** (Hu et al., 2024) makes the
framing explicit: agent *architectures* are programs, so searching for a better
agent is search in code space. Their Meta Agent Search has a meta-agent
program new agents, evaluate them, add them to an archive, and condition the
next proposal on that archive. Discovered agents transferred across domains and
models, which is evidence that harness-level inventions are not model-specific
— an important claim for us, since it means an improvement mined from your
sessions survives your next model upgrade.

**SICA — Self-Improving Coding Agent** (Robeyns et al., 2025) is the closest
existing thing to "this repo, but evolving": an agent whose codebase is its own
target. 17% → 53% on a SWE-bench Verified subset. Its structure is worth
copying wholesale — an archive of (agent version, benchmark results) pairs, and
each iteration selects the best archived agent as the meta-agent that writes
the next one. Explicitly non-gradient: it learns by *reflection plus code
edits*, which is a learning signal that a 1,600-line stdlib-only harness can
actually carry.

The **Survey of Self-Evolving Agents** (2025) gives the taxonomy to file all of
this under — *what* evolves (model / memory / tool / architecture), *when*
(intra-test-time, within one task, vs. inter-test-time, between tasks), *how*
(scalar reward vs. textual feedback; single- vs. multi-agent). Our §2 table is
a "what" axis with blast radius attached; the A/B/C axis is roughly their "how",
with the observation that textual feedback dominates scalar reward for personal
work because scalar reward requires a benchmark.

### 4.3 The skill rung: accumulating capability without touching the loop

**Voyager** (Wang et al., 2023) is the load-bearing precedent for rung 2–3, and
the most directly transferable idea in the entire literature. A Minecraft agent
with three parts: an automatic curriculum, an **ever-growing skill library of
executable code**, and iterative prompting with environment feedback plus
self-verification. Skills are stored as programs, indexed by embedding,
retrieved by semantic similarity, and composed into bigger skills. 3.3x more
unique items, 15.3x faster tech-tree progress.

The crucial architectural point: **Voyager never modifies its own agent loop.**
All accumulated competence lives in a growing library of *data* (code artifacts)
that the fixed loop retrieves and executes. That is rung 3 doing the work of
rung 5, with a fraction of the risk, and it is why skills are the right first
serious target after policy. It also gets compositionality for free — skills
call skills — and sidesteps catastrophic forgetting entirely, because nothing
is overwritten.

Anthropic's **Agent Skills** and Claude Code's **hooks**, **slash commands**,
**subagents** and **plugins** are the same idea shipped: a skill is a folder
with a `SKILL.md` that the harness discovers and loads on demand; a hook is a
shell command the harness runs at a lifecycle event. Note that the
sound-on-completion example is, in Claude Code, precisely a `Stop` hook — a
config edit, not a code change. The lesson is not "copy the format", it is
**the escape hatches are the evolution surface**: once a harness has
config-declared lifecycle hooks and a filesystem-discovered skill/tool
directory, the agent can extend itself by writing files, and rung 5 becomes
unnecessary for a large class of requests. Design the escape hatches first and
most of the self-modification demand routes through them.

### 4.4 The memory rung: evolving the context, not the code

**Reflexion** (Shinn et al., 2023) — convert a failure into a written lesson,
keep it in an episodic buffer, condition the next attempt on it. Verbal
reinforcement learning: no weights move, text does the work of a gradient. This
is rung 0, and it is the cheapest useful thing in the whole space.

**Generative Agents** (Park et al., 2023) — a memory stream with retrieval
scored by recency × importance × relevance, plus periodic *reflection* that
synthesizes higher-level facts from raw observations. The reflection step is
what stops a log from being merely long, and it is the direct ancestor of any
"mine my session history" feature.

**Dynamic Cheatsheet** (2025) and **ACE — Agentic Context Engineering**
(Stanford/SambaNova, 2025) are where this gets rigorous, and ACE names the two
failure modes that will bite any naive implementation:

- **Brevity bias** — summarize-as-you-go quietly discards the specific domain
  detail that was the whole value of the memory.
- **Context collapse** — repeatedly asking a model to rewrite an accumulated
  context degrades it monotonically. The thing eats itself.

ACE's fix is structural: split **Generator** (does the task) / **Reflector**
(extracts the lesson) / **Curator** (merges it in), and make the Curator do
*incremental, itemized, delta* updates — semantic dedup and deterministic
merge, often with no LLM at all — rather than rewriting the playbook wholesale.
Context is a bulleted playbook of discrete items with add/update/remove
operations, not a prose blob.

This is a direct warning to this repo. `context.py` already does
summarize-and-fold compaction, and compaction is *exactly* the lossy rewrite
ACE indicts. **An evolving harness must not store its learned knowledge in
anything that compaction can touch.** Learned state goes in files on disk,
append-structured, item-addressed; the context window is where it is *loaded*,
never where it *lives*.

### 4.5 Prior art that is not about AI at all

The strongest motivations here are older than LLMs, and they are the ones to
lead with when arguing this is a sound architecture rather than a research bet.

- **Emacs.** The canonical answer to "what does a tool look like if users can
  extend it while it runs". A small C core plus a Lisp image you can redefine
  live; `M-x` any function; extensions are data files in a load path.
  Thirty-five years of evidence that *the extensible one wins on longevity*,
  and also that an unmanaged extension surface produces `.emacs` bankruptcy —
  which is the argument for lineage, provenance and pruning, not against
  extensibility.
- **Smalltalk / Lisp images.** The live, self-modifying system. Also the source
  of the cautionary half: an image that has drifted for years is unreproducible
  and unshareable. *Our* answer is that mutations are files in a git repo, so
  drift is a diff.
- **The shell.** The single most successful personalization loop ever
  deployed: `history` → notice repetition → `alias` → promote to a shell
  function → promote to a script on `PATH`. That is rungs 0→2→3, hand-cranked,
  and every developer already understands it. "The harness writes your aliases
  by watching you" is a one-sentence pitch that needs no AI literacy.
- **Adaptive query optimizers** (System R → DB2 LEO → modern cost-model
  feedback). A planner that compares estimated to actual cardinality and
  corrects its own model. Decades of production evidence that a system can
  safely tune *its own policy* from observed execution, as long as the mutation
  is confined to a declarative layer and the executor stays fixed. That is
  precisely the rung 1 / rung 4 boundary.
- **JIT compilers and PGO.** Observe the hot path, specialize for it, keep a
  deoptimization path for when the assumption breaks. The *deopt guard* is the
  transferable idea: every specialization ships with the condition under which
  it must be abandoned.
- **Canary deploys, feature flags, and CI as a fitness function.** The
  software industry's existing answer to "how do you let a change into a running
  system without betting the system on it". Shadow mode, percentage rollout,
  automatic rollback on a regression signal. An evolving harness is a system
  that deploys to itself, and it should borrow the whole apparatus rather than
  inventing a worse one.
- **Autonomic computing** (IBM, 2001) and the **MAPE-K loop** —
  Monitor, Analyze, Plan, Execute over shared Knowledge. This is the reference
  architecture for self-managing systems and it maps cleanly: Monitor = the
  session log, Analyze = mining, Plan = propose a mutation, Execute = write the
  file, Knowledge = the learned-state directory. Twenty-five years of
  self-adaptive-systems literature is filed under this name and it is largely
  unread by the agent community.

---

## 5. What the theory says you must have

Synthesizing across all of the above, four invariants. Any design missing one of
these has a known failure mode with a name.

1. **A fitness signal, or an explicit admission that you don't have one.**
   DGM, ADAS, SICA and AlphaEvolve all rest on a cheap automatic evaluator.
   Personal work has no such evaluator. So for rungs 0–2 the signal is *the
   user did not object, and the mutation is trivially revertible*; for rung 3–4
   it must be an actual test. Never let a mutation land on a fitness signal you
   invented for the occasion — that is how you get the DGM deleting its own
   hallucination detector.
2. **An archive with lineage, not a champion.** Keep every mutation and its
   provenance: what proposed it, from what evidence, when, what it replaced.
   Branch from anywhere. This buys open-endedness (DGM) *and* auditability *and*
   makes "why does my harness do this" answerable — the property that caught the
   DGM's objective hacking.
3. **Reversibility, cheaply and at the granularity of one mutation.** Files in
   git. `/undo` that names the specific rule or skill. A harness you cannot
   un-teach is a harness you will stop teaching.
4. **Containment that the mutation cannot widen.** In this repo, `tools.py`
   containment runs *after* the policy, so no rule can authorize leaving the
   workspace. The same must hold for every new rung: a generated tool is still
   a tool, and still crosses the permission pipeline. The evolution mechanism
   must not be able to grant itself privilege — which specifically means
   **the mutation writer must not be able to edit the permission file's own
   trust anchor, or the containment code, without a human in the loop.**

---

## 6. The hard problems

Honestly stated, because each of these is where an implementation dies.

- **The fitness vacuum.** You cannot A/B a harness on "was Denim's Tuesday
  better". Mitigation: restrict search (**C**) to changes with a computable
  proxy (did the retry succeed, did the tool error rate fall, did the turn cost
  fewer tokens for the same outcome), and take everything else from **A** and
  **B** where the user is the oracle.
- **Context collapse and brevity bias** (§4.4). Mitigation: itemized,
  append-structured learned state; deterministic merges; never store learned
  knowledge anywhere `compact` can reach.
- **Objective hacking** (§4.1). Mitigation: mutations may not edit the
  evaluator, the containment layer, or the trust anchor. Lineage makes it
  visible when they try.
- **Accumulated cruft.** `.emacs` bankruptcy, and its LLM-specific version: a
  hundred stale rules that quietly contradict each other and degrade every
  prompt. Mitigation: every mutation carries a use counter and a last-used
  timestamp; unused mutations are surfaced for pruning; contradictory rules are
  a detectable condition, not a mystery.
- **The trust boundary of a cloned repo.** `policy.json` already lives in the
  workspace, which is why `trusted.json` exists. Every new mutable surface
  inherits that problem *and makes it worse*: a workspace-supplied skill or
  hook is arbitrary code the harness will run. Trust-on-first-use with an
  explicit listing is the existing answer and it must extend to every rung.
- **Mutation cost.** Mining a session log costs tokens. If evolution runs
  inside the turn, the user pays for it in latency on work they didn't ask for.
  Mutation belongs at a boundary — end of turn, end of session, or an explicit
  command — not in the hot loop. (The literature's inter- vs. intra-test-time
  distinction, with a billing argument attached.)
- **Evaluating the meta-system.** How do you know the evolving harness is
  better than the fixed one? The only honest answer is a held-out task set
  replayed against both — which means the harness needs deterministic replay,
  which `session.py` (log is truth, messages are derived) already almost gives
  us. This is an under-appreciated asset.

---

## 7. A staged proposal for macro-harness

In the repo's idiom: one module per layer, stdlib only, small enough to read in
a sitting. Ordered by value-per-risk, and each stage is independently useful —
this is not a plan that only pays off at the end.

Stages 3 and 4 are built; see the README's *Extending a harness that is already
running*. They were taken first, out of order, because between them they cover
"add a capability" and "run something every time" — which is most of what people
actually ask a harness for — and because both are rung 3, where a mutation is
still data and a mistake is still a deleted file.

### Stage 1 — `memory.py` (~90 lines). Rung 0, source A.

An append-only `.macroharness/memory.jsonl` of itemized facts, each with
provenance, a scope (global / this workspace), a use count and a last-used
stamp. Loaded into the system prompt at turn start; **never** inside the
foldable region, so compaction cannot erode it. A `remember` tool lets the
model write one when the user states a durable fact. `/memory` lists and
`/forget <id>` removes.

This is Reflexion plus ACE's itemization, and it is maybe two evenings of work.
It is also the single highest-value item on this list, because "told once,
never re-explained" is most of what "personalized" means to a user.

### Stage 2 — `mine.py` (~120 lines). Rungs 0–1, source B.

Read the session logs — the corpus is already there, typed and append-only, and
nothing currently reads it except `resume`. At end of session (or on `/evolve`),
find the cheap patterns:

- the same `ask` approved N times → propose a policy rule, via the *existing*
  `permissions.derive` so the narrowing discipline is inherited;
- a tool failing the same way repeatedly → propose a memory item;
- a command sequence that recurs → propose a procedure (Stage 4).

Propose, print, confirm, write, log as a `rule`/`memory` event. This is
Generative Agents' reflection step and MAPE-K's Analyze, over a log we already
keep. Note that `always allow` is the N=1 online version of exactly this, so
Stage 2 is a generalization of shipped code, not a new idea.

### Stage 3 — `hooks.py` (~290 lines). Rung 3, source A. **Built.**

`.macroharness/hooks.json`: lifecycle event → shell command. Events:
`pre_tool`, `post_tool`, `tool_error`, `turn_end`, `session_end`, matched with
the same globs and the same `permissions.rule_matches` the policy uses.
Commands run through the same `tools.run_command` as `run_bash` — no new
privilege path, and hooks run *after* the policy, so a hook can only narrow what
was already allowed.

Two of the events are load-bearing rather than decorative. A `pre_tool` hook
marked `blocking` that exits non-zero cancels the call and its output becomes
the tool result; a `post_tool` hook marked `capture` appends its stdout to the
tool result. The second one is the interesting one: a linter that runs after
every write feeds its own errors back into the conversation without the model
having thought to ask. That is the harness teaching the model something within
a single turn.

"Play a sound when the agent finishes" lands here, as a **config edit the agent
can make on request** rather than a code change. Cheapest possible answer to a
large class of "I wish it would also…" asks, and it is the Claude Code hooks
lesson applied directly. `define_hook` is the runtime half.

### Stage 4 — `extensions.py` (~215 lines). Rung 3, sources A and B. **Built.**

Voyager's skill library, filesystem edition, at its simplest useful size.
`.macroharness/tools/<name>.json` holds a JSON Schema and a shell command
template; `define_tool` writes one and registers it mid-turn. Discovered at
startup and registered in the *existing* `Registry`, so an extension tool
crosses the same permission pipeline as a built-in or an MCP tool — `loop.py`
cannot tell them apart, which was the design goal.

The deliberate restriction is that a definition is a **command template, not
Python**. Python would run inside the harness process, outside `Containment`,
and could edit the registry that is supposed to constrain it. A command
inherits the jail. Substitution is a regex plus `shlex.quote` rather than
`str.format`, because `str.format` resolves attributes and a model-supplied
mapping should not get to say `{x.__class__}`. And no extension may shadow an
existing tool: if a definition could redefine `read_file`, every containment
guarantee stated in terms of `read_file` would become a guess.

Still open at this rung: procedures (rung 2) — a `skill.md` describing *when*
to reach for a sequence, rather than a single command — and retrieval once a
few dozen accumulate. Linear scan is correct until it isn't, and the moment it
isn't is a real signal worth measuring rather than pre-optimizing.

### Stage 5 — `strategy.py` (~110 lines). Rung 4, source C.

Now, and only now, the control flow. Extract the currently-hardcoded decisions
into a declarative strategy file: retry classification and backoff, which model
serves which kind of request, whether a write is verified by a second model
before it lands, parallel dispatch width. `model.py` and `loop.py` read the
strategy instead of constants.

Two of the three motivating examples live here — adaptive retry and
route-two-models-and-verify — and both become *config*, at which point the
agent can propose a change to them as a diff on a JSON file. Every strategy
entry carries a deopt guard (§4.5): the condition under which it is abandoned.

### Stage 6 — the archive. Rung 5, source C. Design now, build later.

Only at this point is rung 5 worth discussing, and the honest recommendation is
**don't** — or rather, don't let the running harness edit its own source in the
hot path. Instead take DGM/SICA's structure and put it offline: an archive of
(harness variant, score on a replayed task set), a nightly job that proposes a
source patch, runs the existing 115 tests plus a replay suite, and **opens a
pull request**. Branch from any archived ancestor, not just the champion.

The human-reviewed diff is the "proof" step the Gödel Machine wanted and could
not have. It is also, not coincidentally, how software has always changed.

### What not to build

- No embedding store, no vector DB — for a personal harness this is a linear
  scan over a few hundred items.
- No RL, no weight updates. The learning signal is text and files, which is
  SICA's whole point and the reason this fits in a stdlib-only repo.
- No autonomous rung-5 editing in the live loop, regardless of how well the
  tests pass.
- No storing learned state in the conversation. It collapses (§4.4).

---

## 8. How we would know it worked

The meta-evaluation is the part everyone skips. Three measurable claims,
all computable from the session logs we already write:

1. **Approval rate falls.** Count `ask` prompts per session over time on a
   stable workload. If the harness is learning your habits, this curve goes
   down and the deny rate does not go up.
2. **Restatement rate falls.** How often does the user repeat an instruction
   they have given before? Detectable by mining the logs for near-duplicate
   user turns across sessions.
3. **Held-out replay.** Keep a frozen set of task prompts. Run them against the
   evolved harness and against the Stage-0 harness, same model, same seed
   policy. Compare steps-to-completion, tokens, and tool error rate. This
   requires deterministic replay, which the log-is-truth design already nearly
   affords.

If (1) and (2) improve and (3) does not regress, the harness has become
personal without becoming worse. That is the actual bar, and it is a lower and
more honest bar than "beats SWE-bench".

---

## 9. The one-paragraph version

The harness is frozen because everything it knows was written before the
session started, and nothing it learns survives the session ending. The fix is
not to make the agent rewrite `loop.py`. It is to notice that this repo already
has a working example of harness evolution — `always allow` derives a rule,
prints it, writes it to a committed file, and it takes effect immediately — and
to apply that one mechanism, *propose → show → persist → take effect → account
for*, to four more rungs of mutable surface: facts, procedures, tools and
control flow. The research literature (DGM, ADAS, SICA, AlphaEvolve) has proved
that agents can profitably rewrite agents, and has also proved that they will
hack any objective you hand them, which is an argument for lineage and human
review rather than against the whole idea. The systems literature (Emacs, the
shell, adaptive query optimizers, JIT deopt guards, MAPE-K, canary deploys) has
a thirty-year head start on doing this safely, and its universal answer is:
confine mutation to a declarative layer, keep the executor fixed, make every
change a reviewable artifact, and always keep a way back.

---

## Sources

- [Darwin Gödel Machine (arXiv 2505.22954)](https://arxiv.org/abs/2505.22954) · [Sakana AI writeup](https://sakana.ai/dgm/) · [code](https://github.com/jennyzzt/dgm)
- [A Self-Improving Coding Agent — SICA (arXiv 2504.15228)](https://arxiv.org/abs/2504.15228) · [code](https://github.com/MaximeRobeyns/self_improving_coding_agent)
- [Automated Design of Agentic Systems — ADAS (arXiv 2408.08435)](https://arxiv.org/abs/2408.08435) · [project page](https://www.shengranhu.com/ADAS/)
- [Voyager: An Open-Ended Embodied Agent with LLMs (arXiv 2305.16291)](https://arxiv.org/abs/2305.16291) · [project page](https://voyager.minedojo.org/)
- [Agentic Context Engineering — ACE (arXiv 2510.04618)](https://arxiv.org/abs/2510.04618) · [VentureBeat on context collapse](https://venturebeat.com/ai/ace-prevents-context-collapse-with-evolving-playbooks-for-self-improving-ai)
- [A Survey of Self-Evolving Agents (arXiv 2507.21046)](https://arxiv.org/pdf/2507.21046) · [reading list](https://github.com/CharlesQ9/Self-Evolving-Agents)
- [AlphaEvolve / evolutionary coding agents (IEEE Spectrum)](https://spectrum.ieee.org/evolutionary-ai-coding-agents)
- Reflexion (arXiv 2303.11366); Generative Agents (arXiv 2304.03442); Schmidhuber, Gödel Machines (2003); IBM autonomic computing / MAPE-K (2001)
