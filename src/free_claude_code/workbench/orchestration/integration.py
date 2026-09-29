"""Merge node branches into the base branch in dependency order.

Out-of-scope edits are reverted on the node's own branch first (and reported);
merge conflicts abort that merge (nothing is overwritten) and are reported.
The outcome is at most ``ready_for_verification``: only the verification gate
may mark work VERIFIED.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from free_claude_code.core.json_types import JsonObject

from .graph import TaskGraph, TaskNode, in_scope
from .worktrees import GitError, git


@dataclass
class IntegrationResult:
    merged: list[str] = field(default_factory=list)
    reverted_out_of_scope: dict[str, list[str]] = field(default_factory=dict)
    conflicts: dict[str, list[str]] = field(default_factory=dict)
    final_diff_stat: str = ""

    @property
    def status(self) -> Literal["ready_for_verification", "conflicts"]:
        return "conflicts" if self.conflicts else "ready_for_verification"

    def to_json(self) -> JsonObject:
        return {
            "status": self.status,
            "merged": list(self.merged),
            "reverted_out_of_scope": {
                k: list(v) for k, v in self.reverted_out_of_scope.items()
            },
            "conflicts": {k: list(v) for k, v in self.conflicts.items()},
            "final_diff_stat": self.final_diff_stat,
        }


def enforce_scope(worktree: Path, node: TaskNode) -> list[str]:
    """Revert files the node changed outside its write scope; returns those paths."""

    start = node.start_commit
    if not start:
        return []
    changed = git(worktree, "diff", "--name-only", "--no-renames", start, "HEAD")
    outside = [
        p for p in changed.splitlines() if p and not in_scope(p, node.write_scope)
    ]
    for rel in outside:
        if git(worktree, "ls-tree", "--name-only", start, "--", rel):
            git(worktree, "checkout", start, "--", rel)
        else:
            git(worktree, "rm", "-q", "-f", "--", rel)
    if outside:
        git(
            worktree,
            "commit",
            "-q",
            "-m",
            f"fcc: revert out-of-scope changes of {node.id}: {', '.join(outside)}",
        )
    return outside


def integrate(repo: Path, graph: TaskGraph, base_commit: str) -> IntegrationResult:
    """Merge completed mutating nodes into the checked-out base branch of ``repo``."""

    result = IntegrationResult()
    for node_id in graph.order():
        node = graph.node(node_id)
        if node.status != "completed" or not node.branch:
            continue
        if node.worktree and Path(node.worktree).is_dir():
            node.reverted_out_of_scope += enforce_scope(Path(node.worktree), node)
        if node.reverted_out_of_scope:
            result.reverted_out_of_scope[node.id] = list(node.reverted_out_of_scope)
        try:
            git(
                repo,
                "merge",
                "--no-ff",
                "--no-edit",
                "-m",
                f"fcc: integrate {node.id}",
                node.branch,
            )
            result.merged.append(node.id)
        except GitError as exc:
            files = git(repo, "diff", "--name-only", "--diff-filter=U", check=False)
            git(repo, "merge", "--abort", check=False)
            result.conflicts[node.id] = files.splitlines() or [str(exc)]
    result.final_diff_stat = git(repo, "diff", "--stat", base_commit, "HEAD")
    return result
