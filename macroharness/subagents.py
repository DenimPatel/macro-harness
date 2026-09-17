"""Subagents: one `task` tool that runs a nested loop with a fresh context.

The point is context isolation, not concurrency. A subagent gets its own message
list, the same tools and the same policy, and hands back only its final answer,
so a large search does not have to live in the parent's transcript forever.
"""

from .tools import Tool

TASK_SCHEMA = {
    "type": "object",
    "properties": {
        "prompt": {
            "type": "string",
            "description": "A self-contained task. The subagent cannot see this conversation.",
        },
    },
    "required": ["prompt"],
}

TASK_DESCRIPTION = (
    "Delegate a self-contained subtask to a fresh agent. The subagent has its own "
    "message list, the same tools and the same permissions, and returns only its "
    "final answer. Use it to keep large searches or file reads out of this "
    "conversation. Subagents cannot ask the user anything, and cannot delegate."
)


def task_tool(spawn):
    """Build the `task` tool around a spawn(prompt) -> text callable."""
    def task(prompt):
        return spawn(prompt)
    return Tool("task", TASK_DESCRIPTION, TASK_SCHEMA, task, read_only=False)
