"""Evolver state, kept outside the workspace.

The in-loop agent's file tools are jailed to the workspace. Keeping the replay
set, the ledger and the archive of rejected proposals out here puts them beyond
that reach: the agent being graded cannot read the held-out cases or rewrite
the record of what was applied.

Layout under `~/.macroharness-evolver/<sha(workspace)>/`:

    inbox/<id>.json      proposals: mined, then pending review after the gate
    archive/<id>.json    rejected or failed proposals, kept as negative examples
    replayset.json       named held-out cases with an executable check
    ledger.jsonl         every apply, retire and revert, append-only
    facts.json           accepted facts, until the harness has a memory layer
    metrics.jsonl        one metrics snapshot per ingest
"""

import hashlib
import json
import os
import time

DEFAULT_ROOT = os.path.join(os.path.expanduser("~"), ".macroharness-evolver")


def now_stamp():
    """Sortable against session ids, which start with the same format."""
    return time.strftime("%Y%m%d-%H%M%S")


class Store:
    def __init__(self, workspace, root=None):
        self.workspace = os.path.realpath(workspace)
        if root is None:
            tag = hashlib.sha256(self.workspace.encode("utf-8")).hexdigest()[:16]
            root = os.path.join(DEFAULT_ROOT, tag)
        self.root = root
        for directory in (self.inbox_dir, self.archive_dir):
            os.makedirs(directory, exist_ok=True)

    # --- paths ------------------------------------------------------------------

    @property
    def inbox_dir(self):
        return os.path.join(self.root, "inbox")

    @property
    def archive_dir(self):
        return os.path.join(self.root, "archive")

    @property
    def ledger_path(self):
        return os.path.join(self.root, "ledger.jsonl")

    @property
    def replayset_path(self):
        return os.path.join(self.root, "replayset.json")

    @property
    def facts_path(self):
        return os.path.join(self.root, "facts.json")

    @property
    def metrics_path(self):
        return os.path.join(self.root, "metrics.jsonl")

    # --- json helpers ---------------------------------------------------------------

    @staticmethod
    def read_json(path, default):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, ValueError):
            return default

    @staticmethod
    def write_json(path, value):
        temporary = path + ".tmp"
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True, ensure_ascii=False)
            handle.write("\n")
        os.replace(temporary, path)

    @staticmethod
    def append_jsonl(path, value):
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(value, sort_keys=True, ensure_ascii=False) + "\n")

    @staticmethod
    def read_jsonl(path):
        rows = []
        try:
            with open(path, "r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if line:
                        try:
                            rows.append(json.loads(line))
                        except ValueError:
                            continue
        except OSError:
            pass
        return rows

    # --- proposals ------------------------------------------------------------------

    def _proposals(self, directory):
        found = []
        for entry in sorted(os.listdir(directory)):
            if entry.endswith(".json"):
                data = self.read_json(os.path.join(directory, entry), None)
                if isinstance(data, dict):
                    found.append(data)
        return found

    def inbox(self, status=None):
        rows = self._proposals(self.inbox_dir)
        return [row for row in rows if status is None or row.get("status") == status]

    def archived(self):
        return self._proposals(self.archive_dir)

    def get(self, proposal_id):
        for directory in (self.inbox_dir, self.archive_dir):
            data = self.read_json(os.path.join(directory, proposal_id + ".json"), None)
            if data is not None:
                return data
        return None

    def put(self, proposal):
        self.write_json(os.path.join(self.inbox_dir, proposal["id"] + ".json"), proposal)

    def archive(self, proposal):
        inbox_path = os.path.join(self.inbox_dir, proposal["id"] + ".json")
        self.write_json(os.path.join(self.archive_dir, proposal["id"] + ".json"), proposal)
        if os.path.exists(inbox_path):
            os.remove(inbox_path)

    def remove_from_inbox(self, proposal_id):
        path = os.path.join(self.inbox_dir, proposal_id + ".json")
        if os.path.exists(path):
            os.remove(path)

    # --- ledger -----------------------------------------------------------------------

    def ledger(self):
        return self.read_jsonl(self.ledger_path)

    def append_ledger(self, entry):
        self.append_jsonl(self.ledger_path, entry)

    def active_artifacts(self):
        """Ledger entries still in force: applied, and not since reverted or retired."""
        active = {}
        for entry in self.ledger():
            action = entry.get("action")
            if action == "apply":
                active[entry["id"]] = entry
            elif action in ("revert", "retire"):
                active.pop(entry.get("target_id"), None)
        return active

    # --- replay set, facts, metrics ---------------------------------------------------------

    def replayset(self):
        data = self.read_json(self.replayset_path, {})
        return data if isinstance(data, dict) else {}

    def save_replayset(self, cases):
        self.write_json(self.replayset_path, cases)

    @property
    def held_out_path(self):
        return os.path.join(self.root, "held_out.json")

    def pinned_held_out(self):
        data = self.read_json(self.held_out_path, [])
        return set(data) if isinstance(data, list) else set()

    def pin_held_out(self, session_ids):
        pinned = self.pinned_held_out() | set(session_ids)
        self.write_json(self.held_out_path, sorted(pinned))

    def facts(self):
        data = self.read_json(self.facts_path, [])
        return data if isinstance(data, list) else []

    def save_facts(self, facts):
        self.write_json(self.facts_path, facts)

    def append_metrics(self, snapshot):
        self.append_jsonl(self.metrics_path, snapshot)

    def metrics_history(self):
        return self.read_jsonl(self.metrics_path)
