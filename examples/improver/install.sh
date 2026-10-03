#!/bin/sh
# Install the improver workflow into a repository's .macroharness/ directory.
#
#   examples/improver/install.sh /path/to/repo [--chromium /path/to/chrome]
#
# Copies the agent profiles, the Playwright MCP config, the unattended-run
# policy and the orchestrator prompt. An existing policy.json or mcp.json is
# kept as <name>.bak. The new policy is a different file, so the next
# interactive `mh` run asks you to trust it once; unattended runs refuse an
# untrusted policy.
set -eu

here=$(cd "$(dirname "$0")" && pwd)
target=${1:?usage: install.sh /path/to/repo [--chromium PATH]}
chromium=""
if [ "${2:-}" = "--chromium" ]; then
    chromium=${3:?--chromium needs a path}
fi

state="$target/.macroharness"
mkdir -p "$state/agents" "$state/workflows"
cp "$here"/agents/*.md "$state/agents/"
cp "$here/ORCHESTRATOR.md" "$state/workflows/improver.md"
for name in policy.json mcp.json; do
    if [ -f "$state/$name" ] && ! cmp -s "$here/$name" "$state/$name"; then
        cp "$state/$name" "$state/$name.bak"
        echo "kept your old $name as $name.bak"
    fi
    cp "$here/$name" "$state/$name"
done

if [ -n "$chromium" ]; then
    # Pin a browser that is already installed instead of letting Playwright
    # download the one matching its version.
    python3 - "$state/mcp.json" "$chromium" <<'PY'
import json, sys
path, chromium = sys.argv[1], sys.argv[2]
config = json.load(open(path))
args = config["servers"]["playwright"]["args"]
args += ["--no-sandbox", "--executable-path", chromium]
json.dump(config, open(path, "w"), indent=2)
PY
fi

echo "installed the improver workflow into $state"
echo "next:"
echo "  cd $target && mh            # once, interactively: trust the policy, check /mcp and /agents, then /exit"
echo "  mh --non-interactive --prompt-file .macroharness/workflows/improver.md \\"
echo "     --max-steps 400 --subagent-max-steps 60 --bash-timeout 600 \\"
echo "     --rounds 50 --stop-file .improve/STOP"
echo "  mh-watch                    # in a second terminal"
