# The improver workflow

A long-running, unattended loop that learns a repository, builds a backlog of
improvements, and then implements, independently verifies and commits them one
at a time. An orchestrator does almost no work itself: it delegates to
subagents and keeps its memory in `.improve/`. Changes to anything visual are
verified in a real browser through the Playwright MCP server.

This directory is that workflow packaged for macro-harness:

| File | What it is |
|---|---|
| `ORCHESTRATOR.md` | the orchestrator prompt, written against this harness's tools |
| `agents/*.md` | six subagent roles: `recon`, `experience-auditor`, `backlog`, `implementer`, `verifier`, `regression-sweeper` |
| `mcp.json` | Playwright MCP over stdio, headless, with a 120s per-call timeout |
| `policy.json` | the unattended policy: edits and commands allowed; push, history rewrites, `git add -A` and `.git/` writes denied |
| `install.sh` | copies all of the above into a repository's `.macroharness/` |
| `demo.py` | one full pass against a tiny site, with a scripted model and a real browser |

## The harness features it relies on

- **Named subagents.** `task(prompt, agent="verifier")` runs
  `.macroharness/agents/verifier.md`. Each profile has a header and a body:

  ```markdown
  ---
  description: independent, skeptical verifier
  tools: read_file, run_bash, write_file, playwright__*
  max_steps: 60
  ---
  You are an independent, skeptical reviewer. You did not write this change...
  ```

  `tools` is a list of globs that narrows the parent's tools; it never widens
  the policy. `max_steps` replaces the default subagent step limit. The body
  is appended to the subagent's system prompt. Every subagent starts with a
  fresh context and returns only its final answer.
- **Parallel subagents.** Several `task` calls in one reply run concurrently,
  and their answers come back in call order. Each subagent writes its own log.
- **No recursion.** A subagent's toolset never includes `task`, and a profile
  that lists it does not get it. Only the orchestrator delegates.
- **Playwright through MCP.** Tools are registered as `playwright__browser_*`
  and pass through the same policy as the built-in tools. Screenshots and
  other image blocks are saved to files rather than inlined as base64. The
  server's stderr is drained so it never stalls, requests the server sends
  are answered, and `timeout` is set per server.
- **Long unattended runs.** `--prompt-file` takes the orchestrator prompt.
  `--rounds N` re-enters the same session with "continue" whenever the
  orchestrator ends a turn. `touch .improve/STOP` ends the run cleanly.
  `--bash-timeout` covers slow test suites, and `--subagent-max-steps` gives
  subagents room to work.
- **`mh-watch`.** Watches the run from a second terminal (see below).

## Use it on your repository

```sh
pip install -e /path/to/macro-harness                # gives you mh and mh-watch
/path/to/macro-harness/examples/improver/install.sh /path/to/repo
# with a preinstalled Chromium instead of Playwright's download:
#   install.sh /path/to/repo --chromium /opt/pw-browsers/chromium-1194/chrome-linux/chrome

cd /path/to/repo
mh                    # once, interactively: read and trust the policy,
                      # check /mcp (25 playwright tools) and /agents (6 roles), /exit

export DEEPSEEK_API_KEY=sk-...                       # or --base-url/--model for another provider
mh --non-interactive \
   --prompt-file .macroharness/workflows/improver.md \
   --max-steps 400 --subagent-max-steps 60 --bash-timeout 600 \
   --rounds 50 --stop-file .improve/STOP
```

You can append settings to the prompt as trailing words, for example
`... --stop-file .improve/STOP "FOCUS: accessibility, docs"`.

Then, in a second terminal:

```sh
mh-watch              # live table: every subagent, its state, step, and what it is doing now
mh-watch 7            # follow subagent 7: each tool call, its result, and its reply
mh-watch main         # follow the orchestrator itself
mh-watch --once       # print once instead of refreshing (for scripts or a quick look)
```

```
session 20261003-021216-session   5 subagent(s), 1 running
main: step 5, running task Verify backlog item IMP-001. The site is served at http:/...

#    AGENT                STATE    STEP     TIME     NOW / TASK
1    recon                done     4/40     1s       Static one-page site, no build. Report: ...
     task: Classifier: what is this repository? Write .improve/reports/recon-classifier.md
2    experience-auditor   done     10/60    4s       Two findings: unlabeled Buy button, style.css 404.
...
5    verifier             running  3/60     2s       running playwright__browser_click
     task: Verify backlog item IMP-001. The site is served at http://127.0.0.1:42747/
```

`mh-watch` only reads the session logs and never connects to the running
agent. Each subagent has `start` and `done` events in the parent's log, and
its full transcript is in `sessions/<id>.sub-N.jsonl`. You can attach and
detach whenever you like.

When the run ends, review it with `git log --oneline main..HEAD`, read
`.improve/FINAL_REPORT.md`, and look at the screenshots under
`.improve/artifacts/`. Nothing is pushed: the policy denies `git push`, and the
orchestrator installs a pre-push hook as a second guard.

## See it without an API key

```sh
python3 examples/improver/demo.py --keep --delay 1   # then, in another terminal:
mh-watch --workspace <printed directory>
```

The demo builds a one-page shop whose Buy button has no accessible name,
installs the workflow, and runs one orchestrator turn with a scripted model.
The setup, the parallel recon (with baseline screenshots), the backlog, the
implementer, the browser verifier and the commit are all real; only the
model's choices are scripted. It then checks the outcome against reality
rather than the script: the browser's snapshot shows an unnamed `button`
before and `button "Add to cart"` after, the click updates the page, the PNGs
exist at both widths, there is exactly one commit with a verifier git note,
and no artifacts were committed. If `npx` cannot fetch `@playwright/mcp`, set
`MH_PLAYWRIGHT_MCP` to the command that starts your install.

## Limits worth knowing

- **Verifiers read pages, they do not see them.** The harness speaks text-only
  chat completions, so screenshots are saved for humans to inspect. The
  verifier judges layout, labels and states from the accessibility snapshot,
  which includes element boxes if you pass `boxes: true`. A vision-capable
  model would need image content parts, and those are not wired in.
- **One browser per server.** Parallel subagents share the Playwright server
  and its browser, so keep browser work to one subagent at a time. The prompt
  already does this: only the experience auditor uses the browser during
  parallel recon.
- **The policy is glob matching, not a sandbox.** `*git push*` stops the
  obvious command, not a determined one; the pre-push hook is the backstop.
  `run_bash` runs as you, inside the workspace.
- **"Never edits" is a prompt rule.** A verifier with `run_bash` could still
  write files. If you need that enforced, take `run_bash` out of its `tools`.
