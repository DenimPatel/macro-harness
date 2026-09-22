"""The loop: micro-harness's while-loop with the layers attached.

The two invariants micro states in comments are enforced here, and they are the
reason parallel tool execution is safe:

  1. append the assistant message verbatim, tool_calls and all;
  2. every tool_call gets exactly one matching role:"tool" result, in order.

Parallelism happens *inside* step 2. Results always land in call order, so the
message list looks the same whether the calls ran one at a time or four at once.
"""

import concurrent.futures
import os
import time

from . import context
from . import hooks as hooks_mod
from .permissions import Permissions, primary_arg
from .session import Session
from .tools import decode_args, truncate

MAX_PARALLEL_TOOLS = 4
SUBAGENT_MAX_STEPS = 8

SUBAGENT_SYSTEM_PROMPT = (
    "You are a subagent handling one self-contained task.\n"
    "You have read_file, write_file, edit_file, run_bash and task.\n"
    "You cannot ask the user anything: any call that would need approval is denied.\n"
    "Read before you write, run what you write, and finish by reporting what you "
    "did and what you verified, in a few lines."
)

MAIN_SYSTEM_PROMPT = (
    "You are a coding agent working in the current directory.\n"
    "Your tools are read_file, write_file, edit_file, run_bash and task.\n"
    "Prefer small steps: read before you write, run what you write.\n"
    "Use run_bash for listing files, searching and running things.\n"
    "Some calls need the user's approval; the policy file decides which.\n"
    "When a tool fails, read the error and try a different approach.\n"
    "When you are done, say briefly what you did and what you verified.\n"
)


class Harness:
    def __init__(self, model, registry, permissions, session, accounting, root,
                 system_prompt=MAIN_SYSTEM_PROMPT, max_steps=16, budget=None,
                 interactive=True, sessions_dir=None, out=print, on_text=None,
                 depth=0, hooks=None):
        self.model = model
        self.registry = registry
        self.permissions = permissions
        self.session = session
        self.accounting = accounting
        self.root = root
        self.system_prompt = system_prompt
        self.max_steps = max_steps
        self.budget = budget
        self.interactive = interactive
        self.sessions_dir = sessions_dir or os.path.join(root, ".macroharness", "sessions")
        self.out = out
        self.on_text = on_text
        self.depth = depth
        # A harness with no hooks file still has a Hooks object, so no call site
        # has to branch on whether hooks exist.
        self.hooks = hooks or hooks_mod.Hooks(containment=None)
        self.mcp_servers = []
        self.sub_count = 0
        self.last_reply = ""
        self.last_steps = 0
        self.last_turn_tokens = 0

        self.messages = session.messages()
        if not self.messages:
            # The system prompt is logged like everything else, so a resumed
            # session rebuilds byte-identically and fold indexes stay valid.
            self.messages = [{"role": "system", "content": system_prompt}]
            session.append({"t": "system", "text": system_prompt})

    # --- one user turn ------------------------------------------------------

    def run_user_turn(self, user_text):
        """micro's outer loop body, plus budget, accounting and compaction."""
        start_tokens = self.accounting.total
        # Re-read hooks.json here: a hook added mid-session takes effect on the
        # next turn, and a hook edited on disk re-crosses trust before it runs.
        self.hooks.reload()
        self.messages.append({"role": "user", "content": user_text})
        self.session.append({"t": "user", "text": user_text})
        reply = None
        printed = False
        step = 0

        for step in range(1, self.max_steps + 1):
            if self._budget_reached():
                self.session.append({"t": "budget", "spent": self.accounting.total,
                                     "limit": self.budget})
                reply = "[stopped: token budget reached (%d/%d)]" % (
                    self.accounting.total, self.budget)
                break
            if context.estimate_tokens(self.messages) > context.COMPACT_AT:
                self.compact_context()

            completion = self.model.complete(
                self.messages, self.registry.schemas(), on_text=self.on_text)
            message = completion.message
            self.accounting.add(completion.usage, self.messages, message)

            # Invariant 1: append the assistant message verbatim.
            self.messages.append(message)
            self.session.append({"t": "assistant", "message": message})

            if not message.get("tool_calls"):
                reply = message.get("content") or ""
                if self.on_text is None:
                    self.out("\n" + reply)
                else:
                    self.out("")
                printed = True
                break
            if self.on_text is not None and message.get("content"):
                self.out("")  # end the streamed line before the tool summary
            self._run_tool_calls(message["tool_calls"])
        else:
            reply = "[stopped: reached %d tool rounds]" % self.max_steps

        if reply is None:
            reply = "[stopped: no reply]"
        if not printed:
            self.out(reply)
        self.last_reply = reply
        self.last_steps = step
        self.last_turn_tokens = self.accounting.total - start_tokens
        self.hooks.fire(hooks_mod.TURN_END, reply=reply, steps=step,
                        tokens=self.last_turn_tokens)
        return reply

    def _budget_reached(self):
        return self.budget is not None and self.accounting.total >= self.budget

    # --- tool calls ---------------------------------------------------------

    def _run_tool_calls(self, calls):
        # Authorize everything first, serially and in order: a prompt is a
        # conversation with a human, and humans do not run four at a time.
        plans = []
        for call in calls:
            function = call.get("function") or {}
            name = function.get("name") or ""
            arguments, error = decode_args(function.get("arguments"))
            plans.append({"call": call, "name": name, "arguments": arguments,
                          "error": error, "tool": self.registry.get(name),
                          "arg": "" if error else primary_arg(name, arguments),
                          "decision": None, "blocked": None,
                          "result": None, "ms": None})
        for plan in plans:
            if plan["error"] or plan["tool"] is None:
                continue
            plan["decision"] = self.permissions.authorize(plan["name"], plan["arguments"])

        # pre_tool hooks run serially and in call order, for the same reason
        # authorization does: they have side effects, and a blocking hook is a
        # decision about this call that the next call may depend on. They run
        # after the policy, so a hook can only ever narrow what was allowed.
        for plan in plans:
            if plan["error"] or plan["tool"] is None or not plan["decision"].allowed:
                continue
            outcome = self.hooks.before_tool(plan["name"], plan["arg"], plan["arguments"])
            if outcome.blocked:
                plan["blocked"] = outcome.note

        runnable = [plan for plan in plans if not plan["error"]
                    and plan["tool"] is not None and plan["decision"].allowed
                    and plan["blocked"] is None]
        parallel = [plan for plan in runnable if plan["tool"].read_only]
        if len(parallel) > 1:
            with concurrent.futures.ThreadPoolExecutor(
                    max_workers=min(MAX_PARALLEL_TOOLS, len(parallel))) as pool:
                futures = {pool.submit(self._invoke, plan): plan for plan in parallel}
                for future in concurrent.futures.as_completed(futures):
                    futures[future]["result"] = future.result()
        for plan in plans:
            if plan["result"] is None:
                plan["result"] = self._invoke(plan)

        # post_tool hooks, also serial and in call order, before the result is
        # appended: a captured hook (a formatter, a linter) becomes part of the
        # tool result the model reads, so it must land before invariant 2 does.
        for plan in plans:
            if plan["blocked"] is not None or plan["error"] or plan["tool"] is None:
                continue
            if not plan["decision"].allowed:
                continue
            outcome = self.hooks.after_tool(
                plan["name"], plan["arg"], plan["arguments"], plan["result"])
            if outcome.captured:
                plan["result"] = "%s\n%s" % (plan["result"], outcome.captured)
            if str(plan["result"]).startswith("error:"):
                self.hooks.fire(hooks_mod.TOOL_ERROR, tool=plan["name"],
                                arg=plan["arg"], result=plan["result"])

        # Invariant 2: exactly one result per call, appended in call order.
        for plan in plans:
            content = truncate(str(plan["result"]))
            call_id = plan["call"].get("id") or ""
            self.messages.append({"role": "tool", "tool_call_id": call_id,
                                  "content": content})
            self.session.append({
                "t": "tool", "tool_call_id": call_id, "name": plan["name"],
                "content": content, "ms": plan["ms"],
                "decision": plan["decision"].verb if plan["decision"] else "error",
                "note": plan["decision"].note if plan["decision"] else plan["error"],
            })
            self.out("  %s: %s" % (plan["name"], self._summary(plan)))

    def _invoke(self, plan):
        if plan["error"]:
            return "error: %s" % plan["error"]
        if plan["tool"] is None:
            return "error: unknown tool %r" % plan["name"]
        if not plan["decision"].allowed:
            return "user denied this call (%s)" % plan["decision"].note
        if plan["blocked"] is not None:
            return plan["blocked"]
        started = time.monotonic()
        try:
            result = plan["tool"].func(**plan["arguments"])
        except TypeError as error:
            result = "error: bad arguments for %s: %s" % (plan["name"], error)
        except Exception as error:  # a bad call is a tool result, not a crash
            result = "error: %s: %s" % (type(error).__name__, error)
        plan["ms"] = int((time.monotonic() - started) * 1000)
        return result

    @staticmethod
    def _summary(plan):
        if plan["error"]:
            return "error: %s" % plan["error"]
        if plan["tool"] is None:
            return "unknown tool"
        if not plan["decision"].allowed:
            return "denied (%s)" % plan["decision"].note
        if plan["blocked"] is not None:
            return "blocked by hook"
        duration = "" if plan["ms"] is None else " in %dms" % plan["ms"]
        return "ok%s" % duration

    # --- compaction ---------------------------------------------------------

    def compact_context(self):
        before = context.estimate_tokens(self.messages)
        messages, event = context.compact(
            self.messages, self.model, self.accounting, budget=self.budget)
        if event is None:
            self.out("[compact: nothing safe to fold]")
            return None
        self.messages = messages
        self.session.append(event)
        self.out("[compact: folded %d messages, ~%d -> ~%d tokens]"
                 % (len(event["folded"]), before, event["tokens_after"]))
        return event

    # --- subagents ----------------------------------------------------------

    def spawn_subagent(self, prompt):
        """One nested loop with a fresh message list. Depth is capped at 1."""
        if self.depth >= 1:
            return "error: subagents cannot spawn subagents"
        self.sub_count += 1
        sub_session = Session.at(
            self.sessions_dir, "%s.sub-%d" % (self.session.id, self.sub_count))
        # Same rules, but nobody is there to answer a prompt, so ask is a deny.
        sub_permissions = Permissions(
            self.permissions.policy, self.permissions.path,
            state_dir=self.permissions.state_dir, interactive=False,
            out=self.permissions.out)
        sub = Harness(
            model=self.model,
            registry=self.registry,
            permissions=sub_permissions,
            session=sub_session,
            accounting=self.accounting,  # shared: a subagent cannot outspend the parent
            root=self.root,
            system_prompt=SUBAGENT_SYSTEM_PROMPT,
            max_steps=min(self.max_steps, SUBAGENT_MAX_STEPS),
            budget=self.budget,
            interactive=False,  # nobody to ask, so ask resolves to deny
            sessions_dir=self.sessions_dir,
            out=lambda _line: None,
            on_text=None,
            depth=self.depth + 1,
            hooks=self.hooks,  # a subagent's tool calls cross the same hooks
        )
        self.out("[subagent] %s" % prompt.strip().splitlines()[0][:100])
        reply = sub.run_user_turn(prompt)
        self.out("[subagent] done: %d step(s), %d token(s)"
                 % (sub.last_steps, sub.last_turn_tokens))
        return "%s\n\n(subagent: %d steps, %d tokens)" % (
            reply, sub.last_steps, sub.last_turn_tokens)

    # --- meta ---------------------------------------------------------------

    def close(self):
        self.hooks.fire(hooks_mod.SESSION_END, session=self.session.id)
        for server in self.mcp_servers:
            try:
                server.close()
            except Exception:
                pass
