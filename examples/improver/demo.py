"""One pass of the improver workflow, end to end, with no API key.

    python3 examples/improver/demo.py [--keep] [--delay 1.5]

The model is scripted: it plays the orchestrator and its subagents, so the
run is reproducible and free. Everything else is real: install.sh, the CLI's
wiring (`build`), the policy file, agent profiles, parallel `task` calls, the
git repository, an HTTP server started by an agent, and the Playwright MCP
server driving a real headless Chromium.

What happens, in one orchestrator turn:

  setup         branch, pre-push guard, .improve/ state, artifacts excluded
  recon         `recon` and `experience-auditor` in parallel; the auditor
                serves the site, reads its accessibility tree and saves
                baseline screenshots at 1280px and 390px
  backlog       `backlog` writes FEATURES.md (the auditor found an unlabeled
                Buy button and a 404 for style.css)
  implement     `implementer` fixes IMP-001
  verify        `verifier` reviews the diff, then re-checks in the browser
  commit        one commit, the verifier's summary as a git note

The script then checks the real outcome rather than the scripted claims: the
browser's own snapshot before and after, the screenshots on disk, the commit.

Playwright MCP is started with `npx -y @playwright/mcp@latest` unless
MH_PLAYWRIGHT_MCP holds another command (for instance a local install, plus
`--executable-path` for a preinstalled Chromium). Watch it live from another
terminal with `mh-watch --workspace <the printed directory>`; `--delay` slows
each model call down so there is something to watch.
"""

import argparse
import json
import os
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, REPO)

from macroharness import __main__ as cli  # noqa: E402
from macroharness import permissions  # noqa: E402
from tests.fake_model import RoutingModel, assistant, call  # noqa: E402

SITE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Tiny shop</title>
  <link rel="stylesheet" href="style.css">
</head>
<body>
  <h1>Tiny shop</h1>
  <div class="card">
    <h2>Coffee mug</h2>
    <p>$12</p>
    <button class="buy" onclick="add()"><svg width="16" height="16" viewBox="0 0 16 16"><circle cx="8" cy="8" r="6"/></svg></button>
  </div>
  <p id="status" role="status"></p>
  <script>
    function add() { document.getElementById("status").textContent = "Added to cart"; }
  </script>
</body>
</html>
"""

FEATURES = """# Improvement backlog

### IMP-001 — Give the Buy button an accessible name
- **Status:** TODO
- **Category:** Accessibility
- **Type:** bug-fix
- **Area / files:** site/index.html
- **Intent:** The Buy button holds only an icon, so screen readers announce "button" with no name (seen in the browser snapshot: `button [ref=...]`).
- **Acceptance criteria:**
  1. The accessibility snapshot shows `button "Add to cart"`.
  2. Clicking it still sets the status to "Added to cart".
  3. No other file changes.
- **Verification method:** Playwright: navigate, snapshot, click, screenshot at 1280px and 390px.
- **Effort:** S    **Risk:** low
- **Depends on:** none
- **Priority score:** 25
- **Notes:**

### IMP-002 — Stop requesting the missing style.css
- **Status:** TODO
- **Category:** Correctness
- **Type:** bug-fix
- **Area / files:** site/index.html
- **Intent:** Every page load logs a 404 console error for style.css.
- **Acceptance criteria:**
  1. playwright__browser_console_messages at level "error" is empty after load.
- **Verification method:** Playwright console check.
- **Effort:** S    **Risk:** low
- **Depends on:** none
- **Priority score:** 20
- **Notes:**
"""


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def git(root, *args):
    return subprocess.run(["git", *args], cwd=root, check=True, text=True,
                          capture_output=True).stdout.strip()


def make_repo(root):
    os.makedirs(os.path.join(root, "site"))
    with open(os.path.join(root, "site", "index.html"), "w") as handle:
        handle.write(SITE)
    with open(os.path.join(root, "README.md"), "w") as handle:
        handle.write("# Tiny shop\n\nA one-page static shop. Serve `site/` over http.\n")
    with open(os.path.join(root, ".gitignore"), "w") as handle:
        handle.write(".macroharness/sessions/\n.macroharness/trusted.json\n"
                     ".macroharness/mcp-media/\n.macroharness/*.bak\n")
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "demo@example.com")
    git(root, "config", "user.name", "improver demo")
    git(root, "add", ".")
    git(root, "commit", "-q", "-m", "tiny shop")


def install(root):
    subprocess.run([os.path.join(HERE, "install.sh"), root], check=True,
                   capture_output=True)
    override = os.environ.get("MH_PLAYWRIGHT_MCP")
    if override:
        path = os.path.join(root, ".macroharness", "mcp.json")
        with open(path) as handle:
            config = json.load(handle)
        parts = shlex.split(override)
        server = config["servers"]["playwright"]
        server["command"], server["args"] = parts[0], parts[1:] + [
            "--output-dir", ".improve/artifacts/playwright"]
        with open(path, "w") as handle:
            json.dump(config, handle, indent=2)
    git(root, "add", ".macroharness/agents", ".macroharness/workflows",
        ".macroharness/policy.json", ".macroharness/mcp.json")
    git(root, "commit", "-q", "-m", "install the improver workflow")
    # Stands in for the one interactive `mh` run where a human reads the
    # policy and accepts it. An unattended run refuses an untrusted policy.
    state = os.path.join(root, ".macroharness")
    permissions.mark_trusted(state, permissions.policy_path(state))


def scripts(url, port):
    shots = ".improve/artifacts/%s/%s-%s.png"
    orchestrator = [
        assistant("Phase 1: setup.", tool_calls=[call("run_bash", {"command": (
            "git status --porcelain && git checkout -q -b improve/auto-demo && "
            "mkdir -p .improve/reports .improve/artifacts/baseline && "
            "printf '#!/bin/sh\\necho \"Pushing disabled by improver\"; exit 1\\n' "
            "> .git/hooks/pre-push && chmod +x .git/hooks/pre-push && "
            "echo '.improve/artifacts/' >> .git/info/exclude && "
            "printf 'phase: recon\\nplaywright: yes\\n' > .improve/STATE.md && echo ready")})]),
        assistant("Phase 2: reconnaissance, in parallel.", tool_calls=[
            call("task", {"agent": "recon", "prompt":
                 "Classifier: what is this repository? Write .improve/reports/recon-classifier.md"},
                 "recon-1"),
            call("task", {"agent": "experience-auditor", "prompt":
                 "Experience audit of site/ on port %d. Baselines to .improve/artifacts/baseline/, "
                 "report to .improve/reports/recon-experience.md" % port}, "recon-2"),
        ]),
        assistant("Phase 3: backlog.", tool_calls=[call("task", {"agent": "backlog", "prompt":
                  "Backlog Architect: write .improve/FEATURES.md from .improve/reports/recon-*.md"})]),
        assistant("Phase 4: IMP-001.", tool_calls=[call("task", {"agent": "implementer",
                  "prompt": "Implement backlog item IMP-001."})]),
        assistant(tool_calls=[call("task", {"agent": "verifier",
                  "prompt": "Verify backlog item IMP-001. The site is served at %s" % url})]),
        assistant("PASS: committing IMP-001.", tool_calls=[call("run_bash", {"command": (
            "sed -i '0,/Status:\\*\\* TODO/s//Status:** DONE/' .improve/FEATURES.md && "
            "echo \"$(date -u +%FT%TZ) | IMP-001 | DONE\" >> .improve/PROGRESS.log && "
            "printf 'phase: loop\\ndone: IMP-001\\nnext: IMP-002\\n' > .improve/STATE.md && "
            "printf 'a11y(site): give the Buy button an accessible name [IMP-001]\\n\\n"
            "Why: the icon-only button had no accessible name.\\n"
            "What: aria-label=\"Add to cart\" on the button.\\n"
            "Verified: Playwright snapshot + click at 1280px and 390px, see "
            ".improve/reports/verify-IMP-001.md\\n"
            "Backlog: .improve/FEATURES.md#imp-001\\n' > .improve/artifacts/msg.txt && "
            "git add site/index.html .improve/FEATURES.md .improve/PROGRESS.log "
            ".improve/STATE.md .improve/REPO_PROFILE.md .improve/reports && "
            "git commit -q -F .improve/artifacts/msg.txt && "
            "git notes add -m 'VERDICT: PASS, criteria 1-3 met' HEAD && git log --oneline -1")})]),
        assistant("Committed IMP-001 (verified in the browser at 1280px and 390px). "
                  "IMP-002 is next. Nothing was pushed."),
    ]
    recon = [
        assistant(tool_calls=[call("run_bash", {"command": "ls -R site README.md && cat README.md"})]),
        assistant(tool_calls=[call("write_file", {
            "path": ".improve/reports/recon-classifier.md",
            "content": "# Classifier\n\nStatic one-page site in site/, no build step. "
                       "Verify by serving site/ over http and checking it with Playwright.\n"})]),
        assistant(tool_calls=[call("write_file", {
            "path": ".improve/REPO_PROFILE.md",
            "content": "# Repo profile\n\n- Type: static site\n"
                       "- Serve: python3 -m http.server %d -d site\n"
                       "- Verify: Playwright snapshot, click, 1280px and 390px screenshots\n"
                       "- Pre-existing failures: style.css 404\n" % port})]),
        assistant("Static one-page site, no build. Report: .improve/reports/recon-classifier.md; "
                  "profile written."),
    ]
    auditor = [
        assistant(tool_calls=[call("run_bash", {"command": (
            "nohup python3 -m http.server %d --bind 127.0.0.1 -d site "
            "> .improve/artifacts/server.log 2>&1 & echo $! > .improve/artifacts/server.pid; "
            "for i in 1 2 3 4 5 6 7 8 9 10; do "
            "python3 -c \"import urllib.request; urllib.request.urlopen('%s')\" "
            "2>/dev/null && break; sleep 0.3; done; echo serving" % (port, url))})]),
        assistant(tool_calls=[call("playwright__browser_navigate", {"url": url})]),
        assistant(tool_calls=[call("playwright__browser_snapshot", {})]),
        assistant(tool_calls=[call("playwright__browser_resize", {"width": 1280, "height": 800})]),
        assistant(tool_calls=[call("playwright__browser_take_screenshot", {
            "type": "png", "filename": shots % ("baseline", "home", "desktop")})]),
        assistant(tool_calls=[call("playwright__browser_resize", {"width": 390, "height": 844})]),
        assistant(tool_calls=[call("playwright__browser_take_screenshot", {
            "type": "png", "filename": shots % ("baseline", "home", "mobile")})]),
        assistant(tool_calls=[call("playwright__browser_console_messages", {"level": "error"})]),
        assistant(tool_calls=[call("write_file", {
            "path": ".improve/reports/recon-experience.md",
            "content": "# Experience audit\n\n- The Buy button is icon-only: the snapshot "
                       "shows a button with no name.\n- Console: 404 for style.css.\n"
                       "- Baselines: .improve/artifacts/baseline/home-{desktop,mobile}.png\n"})]),
        assistant("Two findings: unlabeled Buy button, style.css 404. Baselines saved."),
    ]
    backlog = [
        assistant(tool_calls=[call("read_file", {"path": ".improve/reports/recon-experience.md"})]),
        assistant(tool_calls=[call("write_file", {"path": ".improve/FEATURES.md",
                                                  "content": FEATURES})]),
        assistant("2 items (a demo-sized backlog): IMP-001 a11y, IMP-002 correctness."),
    ]
    implementer = [
        assistant(tool_calls=[call("read_file", {"path": ".improve/FEATURES.md"})]),
        assistant(tool_calls=[call("edit_file", {
            "path": "site/index.html",
            "old_string": '<button class="buy" onclick="add()">',
            "new_string": '<button class="buy" onclick="add()" aria-label="Add to cart">'})]),
        assistant(tool_calls=[call("write_file", {
            "path": ".improve/reports/impl-IMP-001.md",
            "content": "# IMP-001\n\nFiles: site/index.html\n"
                       "Approach: aria-label on the icon-only button.\n"})]),
        assistant("STATUS: done\nFILES: site/index.html\nSUMMARY: aria-label=\"Add to cart\"\n"
                  "VERIFY: snapshot shows button \"Add to cart\""),
    ]
    verifier = [
        assistant(tool_calls=[call("run_bash", {"command": "git diff --stat && git diff site/"})]),
        assistant(tool_calls=[call("playwright__browser_navigate", {"url": url})]),
        assistant(tool_calls=[call("playwright__browser_snapshot", {})]),
        assistant(tool_calls=[call("playwright__browser_click", {
            "element": "Add to cart button", "target": "button.buy"})]),
        assistant(tool_calls=[call("playwright__browser_snapshot", {})]),
        assistant(tool_calls=[call("playwright__browser_resize", {"width": 1280, "height": 800})]),
        assistant(tool_calls=[call("playwright__browser_take_screenshot", {
            "type": "png", "filename": shots % ("IMP-001", "home", "desktop")})]),
        assistant(tool_calls=[call("playwright__browser_resize", {"width": 390, "height": 844})]),
        assistant(tool_calls=[call("playwright__browser_take_screenshot", {
            "type": "png", "filename": shots % ("IMP-001", "home", "mobile")})]),
        assistant(tool_calls=[call("write_file", {
            "path": ".improve/reports/verify-IMP-001.md",
            "content": "# Verify IMP-001\n\nVERDICT: PASS\n1. snapshot: button \"Add to cart\"\n"
                       "2. click sets status \"Added to cart\"\n3. diff touches site/index.html only\n"
                       "Console: style.css 404 is pre-existing (IMP-002).\n"})]),
        assistant(tool_calls=[call("run_bash", {
            "command": "kill $(cat .improve/artifacts/server.pid) && echo stopped"})]),
        assistant("VERDICT: PASS\nCRITERIA: 1, 2, 3 met\n"
                  "EVIDENCE: .improve/reports/verify-IMP-001.md, .improve/artifacts/IMP-001/"),
    ]
    # Routed by a phrase in each conversation's first message. The orchestrator
    # key comes first: its prompt mentions every other role.
    return {
        "# Autonomous Repository Improver": orchestrator,
        "Classifier:": recon,
        "Experience audit": auditor,
        "Backlog Architect": backlog,
        "Implement backlog item IMP-001": implementer,
        "Verify backlog item IMP-001": verifier,
    }


class SlowModel(RoutingModel):
    def __init__(self, scripts, delay):
        super().__init__(scripts)
        self.delay = delay

    def complete(self, messages, tools, on_text=None):
        time.sleep(self.delay)
        return super().complete(messages, tools, on_text)


def check(root, harness):
    """Assert the real outcome, not the scripted narration."""
    sessions = harness.sessions_dir
    logs = {}
    for name in os.listdir(sessions):
        with open(os.path.join(sessions, name)) as handle:
            logs[name] = [json.loads(line) for line in handle if line.strip()]
    sub = {event["agent"]: event["log"] for event in logs[harness.session.id + ".jsonl"]
           if event.get("t") == "subagent"}

    def results(agent, tool):
        return [event["content"] for event in logs[sub[agent]]
                if event.get("t") == "tool" and event.get("name") == tool]

    before = results("experience-auditor", "playwright__browser_snapshot")[0]
    after = results("verifier", "playwright__browser_snapshot")[0]
    clicked = results("verifier", "playwright__browser_snapshot")[1]
    checks = [
        ("browser saw an unnamed button before", "- button [ref=" in before),
        ("browser sees button \"Add to cart\" after", 'button "Add to cart"' in after),
        ("after the click the page says \"Added to cart\"", "Added to cart" in clicked),
        ("parallel recon: both subagents logged", {"recon", "experience-auditor"} <= set(sub)),
        ("no subagent had the task tool",
         all("task" not in [t for e in logs[log] if e.get("t") == "system"
                            for t in e["text"].split("Your tools are: ")[1].split(".")[0].split(", ")]
             for log in sub.values())),
        ("pre-push guard installed",
         os.access(os.path.join(root, ".git", "hooks", "pre-push"), os.X_OK)),
    ]
    for kind in ("baseline", "IMP-001"):
        for width in ("desktop", "mobile"):
            path = os.path.join(root, ".improve", "artifacts", kind, "home-%s.png" % width)
            ok = os.path.exists(path) and open(path, "rb").read(4) == b"\x89PNG"
            checks.append(("%s %s screenshot is a PNG" % (kind, width), ok))
    subject = git(root, "log", "-1", "--format=%s")
    files = git(root, "show", "--name-only", "--format=", "HEAD").split()
    checks.append(("one commit for IMP-001", subject.endswith("[IMP-001]")))
    checks.append(("artifacts were not committed", not any("artifacts" in f for f in files)))
    checks.append(("verifier note attached", "PASS" in git(root, "notes", "show", "HEAD")))
    checks.append(("on the work branch", git(root, "branch", "--show-current") == "improve/auto-demo"))
    return checks, files


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--workspace", help="where to create the demo repo (default: a temp dir)")
    parser.add_argument("--keep", action="store_true", help="keep the demo repo afterwards")
    parser.add_argument("--delay", type=float, default=0.0,
                        help="seconds per model call, so mh-watch has something to show")
    args = parser.parse_args()

    root = os.path.realpath(args.workspace or tempfile.mkdtemp(prefix="improver-demo-"))
    os.makedirs(root, exist_ok=True)
    port = free_port()
    url = "http://127.0.0.1:%d/" % port
    print("demo repository: %s" % root)
    print("watch it:        mh-watch --workspace %s\n" % root)
    make_repo(root)
    install(root)

    with open(os.path.join(root, ".macroharness", "workflows", "improver.md")) as handle:
        prompt = handle.read()
    cli_args = cli.build_parser().parse_args([
        "--workspace", root, "--non-interactive", "--max-steps", "20",
        "--subagent-max-steps", "30", "--bash-timeout", "120"])
    model = SlowModel(scripts(url, port), args.delay)
    harness = cli.build(cli_args, model=model)
    try:
        cli.run_rounds(harness, prompt, rounds=1, stop_file=".improve/STOP")
    finally:
        harness.close()
        pid_file = os.path.join(root, ".improve", "artifacts", "server.pid")
        if os.path.exists(pid_file):
            subprocess.run("kill $(cat %s) 2>/dev/null" % pid_file, shell=True)

    checks, files = check(root, harness)
    print("\n== real outcome ==")
    for label, ok in checks:
        print("  [%s] %s" % ("ok" if ok else "FAIL", label))
    print("\ngit log --oneline main..HEAD")
    print("  " + git(root, "log", "--oneline", "main..HEAD").replace("\n", "\n  "))
    print("committed files: %s" % ", ".join(files))
    print("\nreview the subagents:  mh-watch --workspace %s --once" % root)
    print("replay the verifier:   mh-watch --workspace %s 5" % root)
    if not args.keep and not args.workspace:
        shutil.rmtree(root)
        print("(demo repository removed; pass --keep to inspect it)")
    return 0 if all(ok for _, ok in checks) else 1


if __name__ == "__main__":
    sys.exit(main())
