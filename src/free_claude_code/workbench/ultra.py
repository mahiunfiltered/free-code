"""Ultra mode's lead-agent report: sub-agent results -> one prompt for the main chat.

The main chat session never saw the orchestrated request, so the report carries it
verbatim inside ``<user_request>``. The whole block is wrapped in
``<fcc_ultra_report task_id=...>`` so the chat UI can render it as a collapsed
"reports" chip instead of a user message.
"""

from collections.abc import Sequence

from free_claude_code.workbench.orchestration.graph import TaskGraph
from free_claude_code.workbench.orchestration.integration import IntegrationResult

REPORT_TAG = "fcc_ultra_report"
_REPORT_LIMIT = 1_500


def synthesis_prompt(
    request: str,
    graph: TaskGraph,
    integration: IntegrationResult | None,
    kept_worktrees: list[str],
    *,
    task_id: str,
    workspace: str,
    ignored: Sequence[str] = (),
) -> str:
    """The lead agent's final-answer prompt: request, per-sub-task reports, integration."""

    where = {
        "snapshot": "were applied to the working tree",
        "in_place": "were made directly in the project folder",
    }.get(workspace, "were merged")
    lines = [
        f'<{REPORT_TAG} task_id="{task_id}">',
        "<user_request>",
        request,
        "</user_request>",
        "",
        f"You are the lead agent. You split the request above into {len(graph.nodes)} "
        f"sub-tasks and your sub-agents have finished; their changes {where}.",
        "Sub-agent reports:",
    ]
    for node in graph.nodes:
        lines += ["", f"## {node.id} ({node.role}) - {node.status}"]
        lines.append(f"Objective: {node.objective}")
        if node.changed_files:
            lines.append(f"Files changed: {', '.join(node.changed_files)}")
        if node.reverted_out_of_scope:
            lines.append(
                "Out-of-scope edits reverted: " + ", ".join(node.reverted_out_of_scope)
            )
        if node.out_of_scope:
            lines.append(
                "Changed outside every sub-agent's scope (left in place): "
                + ", ".join(node.out_of_scope)
            )
        if node.error:
            lines.append(f"Error: {node.error}")
        if node.summary:
            lines.append(f"Report: {node.summary[:_REPORT_LIMIT]}")
    if integration is not None:
        lines += ["", "Integration:"]
        if integration.merged:
            lines.append(f"- applied: {', '.join(integration.merged)}")
        for node_id, files in integration.conflicts.items():
            lines.append(
                f"- CONFLICT, not applied for {node_id}: {', '.join(files)} "
                "(the user changed these files too)"
            )
    if ignored:
        lines.append(
            "Ignored tool-state files (not applied, not a sub-agent's work): "
            + ", ".join(ignored)
        )
    if kept_worktrees:
        lines.append(
            "Unapplied work is kept for manual review in: " + ", ".join(kept_worktrees)
        )
    lines += [
        f"</{REPORT_TAG}>",
        "",
        "Now write the final answer to the user: what was done (briefly, per "
        "sub-task), which files changed, anything that failed or conflicted and "
        "needs their attention, and how to run or check the result. Answer from "
        "these reports: do not re-read, re-test or redo work a sub-agent completed. "
        "Only if a sub-task failed or clearly missed a requested deliverable, do "
        "that one missing piece yourself, then answer.",
    ]
    return "\n".join(lines)
