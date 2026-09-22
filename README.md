# macro-harness

micro-harness is an agent in one file: a while-loop around a chat completion that
is allowed to call functions. It is honest about the twelve things it leaves out.

macro-harness puts those twelve back — one module each, still small enough to
read in a sitting, still standard library only. Nothing here is production grade
and nothing is tuned for speed. The point is to see what each layer actually
costs and where the seams belong.

The thesis is unchanged: **an agent is a while-loop around a chat completion that
is allowed to call functions.** Every module below is one answer to "what else
does that loop need before you can leave it running on its own?"

## Run it

```sh
export DEEPSEEK_API_KEY=sk-...

# Run these two once, so the module resolves from any directory:
python3 -m venv /tmp/macroharness-venv
/tmp/macroharness-venv/bin/pip install -e /path/to/macro-harness

mkdir /tmp/scratch && cd /tmp/scratch
/tmp/macroharness-venv/bin/python -m macroharness
```

The editable install is what lets the module resolve from a scratch directory;
without it (or `PYTHONPATH=/path/to/macro-harness`) the package is only
importable from the repo root. A venv sidesteps the PEP 668 error from a
Homebrew or otherwise externally-managed Python — on an unmanaged interpreter,
`pip install -e /path/to/macro-harness` is enough.

The agent is confined to the workspace directory (the current directory by
default), and its state lives there in `.macroharness/`. Any OpenAI-shaped
`/chat/completions` endpoint works: change `--base-url` and `--model`.

`mh` is the same entry point as a console script, and a trailing prompt makes it
one-shot instead of interactive. The workspace is still the current directory, so
run it from whatever repository you want it to see:

```sh
# put the bin dir your install used on PATH, once
export PATH="/tmp/macroharness-venv/bin:$PATH"

mh summarize this repo    # one turn, then exit
mh                        # no prompt: the REPL
```

```
you> list the files here and tell me what this project is

  run_bash: ok in 12ms
[answer]

you> /tokens
calls: 2
prompt tokens: 3,481 (last request: 1,904)
completion tokens: 260
total: 3,741
estimated cost: $0.0012
```

## Anatomy

| Module | Lines | What it owns |
|---|---|---|
| `__main__.py` | ~200 | flags, the REPL, the one-shot `mh` entry point, `/compact /tokens /session /rules /exit`, wiring |
| `loop.py` | ~260 | the two loops, invariants, parallel dispatch, subagent spawning |
| `model.py` | ~200 | one POST or one SSE stream, retries, usage |
| `session.py` | ~110 | the append-only JSONL log, and the replay that reads it |
| `context.py` | ~155 | token accounting, cost, `fold_boundary`, compaction |
| `tools.py` | ~215 | the registry, read/write/edit/bash, truncation, containment |
| `permissions.py` | ~265 | policy file, rule matching, the three-way prompt, rule derivation |
| `subagents.py` | ~35 | the `task` tool |
| `mcp.py` | ~170 | stdio JSON-RPC client, `tools/list` and `tools/call` |
| `hooks.py` | ~290 | lifecycle hooks: `pre_tool`, `post_tool`, `tool_error`, `turn_end`, `session_end` |
| `extensions.py` | ~215 | tools defined as data, loaded at startup or added mid-turn |

micro's split was one file because the loop is the whole idea. macro's split is
one file per layer because the boundaries between layers are the whole idea.

## The loop, in prose

Unchanged from micro, and worth repeating because everything else hangs off it.
`messages` is the agent's entire memory. A user turn is appended, then the
harness runs at most `max_steps` rounds. Each round calls the model with the
whole list and appends the assistant message **verbatim**. If there are no
`tool_calls`, the turn is over. Otherwise every call is answered by exactly one
`role: "tool"` message carrying its `tool_call_id`.

Three invariants hold across every layer:

1. The assistant message is appended verbatim, `tool_calls` and all.
2. Every `tool_call` gets exactly one matching result, appended in call order —
   including when the calls ran in parallel.
3. `messages` is append-only during a turn. Only compaction folds it, and only
   at a boundary that does not split a `tool_call` from its results.

Invariant 3 is the subtle one. A summarizer that folds half a tool pair produces
a list the provider rejects, so `context.fold_boundary` walks the boundary
forward until the pair is whole, and compaction is skipped if no safe boundary
exists. Skipping is the correct failure mode.

## What micro left out, and what macro does

| micro's omission | macro's answer | where |
|---|---|---|
| Streaming | SSE by default: text prints as it arrives, tool-call fragments are reassembled into the same message the blocking endpoint would have returned | `model.py` |
| Retries / backoff | 429, 5xx, `URLError` and timeouts retry up to 3 times with `min(2**n, 8)s` + jitter, honouring `Retry-After`. A stream that already printed text is not retried | `model.py` |
| Context compaction | `/compact` or auto at 75% of the context limit: fold everything but the last 6 messages into one summary, never splitting a tool pair, and record the fold in the log | `context.py` |
| A permissions policy | `policy.json`: an ordered rule list, first match wins, three verbs. Unknown tools fall through to whatever the file says, and no match is a deny | `permissions.py` |
| Sandboxing | the workspace is a jail: `realpath` resolution refuses `..` and symlink escapes, `run_bash` gets a scrubbed env, a timeout, and a killed process group. Containment, not isolation | `tools.py` |
| Session persistence | an append-only JSONL log. The message list is derived by replaying it, which is why resume and compaction agree | `session.py` |
| Subagents | a `task` tool that runs a nested loop with a fresh context, depth 1, sharing the parent's token budget. Ask degrades to deny inside it | `subagents.py` |
| MCP | stdio JSON-RPC: `initialize`, `tools/list`, `tools/call`. Server tools are namespaced `server__tool` and registered in the same registry as the built-ins, so they cross the same permission pipeline | `mcp.py` |
| Parallel tool execution | calls that are allowed and read-only run in a small thread pool; everything else stays sequential. Results are appended in call order regardless of completion order | `loop.py` |
| Token accounting | provider `usage` when present, `chars / 4` when not, running totals, an estimated cost, and `--budget N` which stops the turn | `context.py` |
| Prompt caching | **not implemented.** See below | — |
| Structured file editing | `edit_file` with a uniqueness check, plus `offset`/`limit` on `read_file` | `tools.py` |
| A harness you can extend while it runs | new tools and lifecycle hooks, declared as data, added at startup **or** during a turn | `extensions.py`, `hooks.py` |

## The policy file

micro's entire safety story is one y/n prompt. macro's is a file you can read,
diff and commit: `.macroharness/policy.json`, created with a conservative default
on first run.

```json
{
  "version": 1,
  "rules": [
    {"tool": "read_file", "arg": "*",      "verb": "allow"},
    {"tool": "run_bash",  "arg": "ls *",   "verb": "allow"},
    {"tool": "run_bash",  "arg": "git diff *", "verb": "allow"},
    {"tool": "write_file","arg": "*.env",  "verb": "deny"},
    {"tool": "*",         "arg": "*",      "verb": "ask"}
  ]
}
```

- `tool` is a glob against the tool name, so `*` also covers MCP tools.
- `arg` is a glob against the one argument that matters: the command for
  `run_bash`, the path for file tools, the serialized arguments otherwise.
  A rule written `"git status *"` also matches a bare `git status`.
- First match wins, in file order. The default ends with an explicit ask-all
  rule, so the fallback is visible in the file rather than hidden in code.
- No matching rule denies. A malformed policy refuses to run.
- `glob`'s `*` spans `/`, so path rules are coarse on purpose. Containment runs
  *after* the policy: no rule can authorize leaving the workspace.

When a call hits an `ask` rule, the prompt offers three answers:

```
approve run_bash git status --short?
  [y] allow once   [a] always allow (run_bash "git *")   [d] deny >
```

`always allow` derives a scoped rule, **prints it first**, and inserts it at the
top of the file so it takes effect immediately. The derivation refuses to widen
across shell metacharacters (`ls; rm -rf /` stays exact), and file rules narrow
to the extension (`src/main.py` becomes `src/*.py`).

The policy file lives inside the workspace, so a cloned repository could ship a
permissive one. macro therefore trusts on first use: rules are listed once and
the hash is remembered in `.macroharness/trusted.json`. Silent trust is the thing
to avoid, not trust itself.

## Extending a harness that is already running

The twelve layers above were all written before your session started. These two
are the seam that lets a session add to them, and the design rule is that there
is **no separate path for a built-in and an added capability**: both end at
`registry.register(Tool(...))`, so by the time `loop.py` dispatches a call it
cannot tell which is which. Two registration paths would make the second one
second class, and the second one is the whole point.

### Tools, as data

An extension tool is a shell command template with a JSON Schema on the front:

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

A template, not Python: Python would run inside the harness process, outside
`Containment`, and could edit the registry that is supposed to constrain it. A
command goes through the same `tools.run_command` as `run_bash`, so it inherits
the workspace jail, the timeout and the scrubbed environment — and it still
crosses the permission policy on every call, because nothing downstream knows
it was added late.

`{placeholder}` substitution is a regex plus `shlex.quote`, not `str.format`:
`str.format` resolves attributes, so `{path.__class__}` on a model-supplied
mapping is a sentence in a language we did not mean to accept. Every
placeholder must be a declared parameter, and no extension may shadow an
existing tool — if a definition could redefine `read_file`, every containment
guarantee stated in terms of `read_file` would be a guess.

The `define_tool` tool writes one of these and registers it **immediately**, so
a tool invented on step 1 is callable on step 2 of the same turn. `loop.py` asks
the registry for schemas on every step rather than caching them at startup,
which is the one-line reason this works at all.

### Hooks

A tool is something the *model* decides to call. A hook is something the
*harness* runs whether the model likes it or not, at a point the model cannot
see. That is where the non-negotiable rules go.

```json
.macroharness/hooks.json
{
  "version": 1,
  "hooks": [
    {"event": "pre_tool",  "tool": "write_file", "arg": "*",
     "run": "git check-ignore -q {arg} && exit 1 || exit 0", "blocking": true},
    {"event": "post_tool", "tool": "write_file", "arg": "*.py",
     "run": "python3 -m py_compile {arg}", "capture": true},
    {"event": "turn_end",  "run": "afplay /System/Library/Sounds/Glass.aiff"}
  ]
}
```

Five events — `pre_tool`, `post_tool`, `tool_error`, `turn_end`, `session_end`
— matched with the same globs and the same `rule_matches` the policy uses, so
there is one matching language in the harness rather than two.

Two of the five can change what the model sees, which is what makes hooks more
than notifications:

- a `pre_tool` hook with `"blocking": true` that exits non-zero **cancels the
  call**, and its output becomes the tool result. The model reads the refusal
  and adapts, exactly as it would any other tool failure.
- a `post_tool` hook with `"capture": true` **appends its stdout to the tool
  result**. A formatter or linter that runs after every write feeds its own
  errors back into the conversation without the model having thought to ask.

Hooks run *after* the policy, so a hook can only ever narrow what was already
allowed. They run serially and in call order, for the same reason authorization
does: they have side effects, and a blocking hook is a decision the next call
may depend on. Both loop invariants survive — a blocked call still gets exactly
one `role: "tool"` result, in call order.

Context reaches a hook as `MH_EVENT`, `MH_TOOL`, `MH_ARG` and friends, and as
the whole call as JSON on stdin. `hooks.json` is re-read at the start of every
turn, so a hook added mid-session takes effect on the next turn with no restart.

`define_hook` is the runtime half, the same way `define_tool` is for tools.

### Trust

Both files live in the workspace, so a cloned repository could ship a tool
definition or a hook that this harness would then execute. That is the exact
problem `policy.json` already had, so it gets the same answer: `trusted.json`
now holds one digest per file, each accepted once, printed in full before it is
accepted. **Editing a hook file revokes its trust**, which is the point rather
than an annoyance — the digest that was accepted is the only version that may
run. A hook or tool the agent wrote through `define_hook` / `define_tool`
crossed the permission prompt on the way in, and is marked trusted there, so it
does not ask twice.

`--no-evolve` leaves `define_tool` and `define_hook` unregistered, for a session
that should not be able to extend itself.

## State on disk

```
.macroharness/
  policy.json        committed; the rule list
  hooks.json         committed; the lifecycle hooks
  mcp.json           committed; stdio servers. {"servers": {}}
  tools/*.json       committed; one extension tool each
  trusted.json       the hashes this user accepted, one key per file
  sessions/*.jsonl   append-only; gitignored. Subagents write <id>.sub-N.jsonl
  .gitignore         "sessions/"
```

Session events are typed: `system`, `user`, `assistant`, `tool`, `compact`,
`budget`, `rule`. Resume does not restore a snapshot; it replays the log, so a
compacted session resumes compacted.

## Meta commands

| Command | What it does |
|---|---|
| `/compact` | fold the old part of the context now, and say what it folded |
| `/tokens` | calls, prompt and completion totals, last request size, estimated cost, how close compaction is |
| `/session` | session id, log path, message count, subagent count |
| `/rules` | the live rule list, in evaluation order |
| `/tools` | every registered tool, built-in, MCP and extension alike |
| `/hooks` | the live hook list, re-read from disk |
| `/exit` | leave. Ctrl-C during a turn keeps the session |

## Tests

No API key, no network, no spend. A scripted model stands in for the provider,
including malformed tool calls, retryable errors and split SSE chunks.

```sh
python3 -m unittest discover -s tests -t . -v
```

169 tests cover: verbatim append and tool-call pairing, tool errors as results,
the step cap, rule precedence, ask-to-deny without a human, always-allow
persistence, workspace and symlink escapes, edit uniqueness, fold boundaries that
never split a pair, compaction replay, resume, budget stops, parallel result
ordering, subagent isolation and depth refusal, MCP discovery through the
policy, and the one-shot `mh` invocation. `tests/fake_mcp_server.py` is a real
MCP server over stdio.

## Live smoke checklist

Run these by hand in a scratch directory when you change the loop:

1. Ask it to create and run a small script — read, write, approval, bash.
2. Ctrl-C mid-turn, then `python3 -m macroharness --resume` — the transcript returns.
3. `/compact` on a long session, then `/tokens` — the fold is logged and accounted.
4. Add a server to `.macroharness/mcp.json`, restart, and call one of its tools.
5. Ask it for a capability it does not have ("add a tool that counts lines in a
   file") and then use that tool in the same turn.
6. Ask it to notify you when a turn ends, then run another turn.

## What is still missing

- **Prompt caching.** The full message list is resent every step, so every step
  re-bills the whole prefix. Fixing it means stable prefixes and provider-specific
  cache breakpoints (`cache_control`), and compaction is hostile to both: it
  rewrites the prefix. A real harness places the breakpoint before the fold, or
  caches the summary as a stable unit. It is left out because it is provider
  plumbing, not a loop concept.
- **A real sandbox.** `run_bash` is contained, not isolated: it runs as you, on
  your machine. This is exactly why it defaults to `ask`. Real isolation needs
  containers or a syscall filter, which is a different project.
- **Everything deepseek-harness already does.** No DI, no web UI, no
  multi-provider abstraction, no durable background jobs, no resources or
  notifications beyond MCP tools, no Windows process-group semantics.
- **The rest of the evolution surface.** Tools and hooks are two rungs of a
  taller ladder: durable facts, mined proposals, saved procedures, and the
  control-flow decisions still hardcoded in `model.py` and `loop.py` (retry
  classification, model routing, verify-before-write). `docs/evolving-harness.md`
  works through what each rung costs and what the prior art says about doing it
  without the harness quietly rotting.

## The three rungs

- **micro-harness** — the loop, one file. Read this first.
- **macro-harness** — this repo. The loop plus its layers, one module each.
- **deepseek-harness** — the same parts scaled up and made replaceable: the loop
  as `core/agent-loop`, the message list as the session log, tool dispatch as a
  guarded pipeline behind `ctx.tools`.

If you are reading them in order: micro tells you what an agent *is*, macro tells
you what it *needs*, and deepseek-harness tells you what it *costs* to make it
survive real users.
