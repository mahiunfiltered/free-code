"""Run a TaskGraph as concurrent Claude Code sessions, then integrate the branches.

Mutating nodes run in their own git worktree/branch (write-scope leases serialize
overlapping scopes); read-only nodes run in the main repo in ``plan`` mode.
Progress streams from :meth:`Orchestrator.run` as JSON events (see ``_TERMINAL``
and the ``type`` values emitted below).
"""

import asyncio
import contextlib
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from pathlib import Path

from loguru import logger

from free_claude_code.cli.managed.interactive import (
    ChatSessionError,
    InteractiveClaudeSession,
    InteractiveClaudeSessions,
)
from free_claude_code.core.json_types import JsonObject

from .graph import TaskGraph, TaskNode
from .integration import IntegrationResult, enforce_scope, integrate
from .leases import Lease, LeaseManager
from .worktrees import (
    GitError,
    commit_all,
    create_worktree,
    current_branch,
    git,
    remove_worktree,
    require_clean_repo,
)

# Max concurrent nodes per routing strategy.
PARALLELISM = {"economy": 1, "balanced": 3, "fastest": 6}
_TERMINAL = frozenset({"node_completed", "node_failed", "node_cancelled"})
_SUMMARY_LIMIT = 4_000


class NodeFailedError(RuntimeError):
    """Claude finished the node without a successful result."""


@dataclass
class OrchestratorOptions:
    permission_mode: str = "acceptEdits"
    model: str | None = None
    max_parallel: int = PARALLELISM["balanced"]
    node_timeout_s: float = 900.0
    max_turns: int = 30
    # Intent contract rendered as text; copied verbatim into every node prompt.
    contract_block: str | None = None
    interrupt_timeout_s: float = 10.0


class Orchestrator:
    def __init__(
        self,
        graph: TaskGraph,
        repo: Path | str,
        sessions: InteractiveClaudeSessions,
        options: OrchestratorOptions | None = None,
    ) -> None:
        self.graph = graph
        self.repo = Path(repo).resolve()
        self.sessions = sessions
        self.options = options or OrchestratorOptions()
        self.integration: IntegrationResult | None = None
        self.base_commit = ""
        self._leases = LeaseManager()
        self._held: dict[str, Lease | None] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._live: dict[str, InteractiveClaudeSession] = {}
        self._queue: asyncio.Queue[JsonObject] = asyncio.Queue()
        self._cancelled = False

    # ----- public ------------------------------------------------------------

    async def cancel(self) -> None:
        """Interrupt and close every running node; pending nodes are cancelled."""

        if self._cancelled:
            return  # a second cancel would abort the sessions' interrupt/close cleanup
        self._cancelled = True
        for task in list(self._tasks.values()):
            task.cancel()

    async def run(self) -> AsyncGenerator[JsonObject]:
        graph = self.graph
        if errors := graph.validate():
            yield {"type": "orchestration_failed", "error": f"invalid graph: {errors}"}
            return
        try:
            self.base_commit = await asyncio.to_thread(require_clean_repo, self.repo)
            base_branch = await asyncio.to_thread(current_branch, self.repo)
        except GitError as exc:
            yield {"type": "orchestration_failed", "error": str(exc)}
            return
        yield {
            "type": "orchestration_started",
            "task_id": graph.task_id,
            "base_commit": self.base_commit,
            "base_branch": base_branch,
            "graph": graph.to_json(),
        }
        try:
            async for event in self._run_nodes():
                yield event
        finally:
            if self._tasks:  # consumer stopped early: never leak sessions
                await self.cancel()
                await asyncio.gather(*self._tasks.values(), return_exceptions=True)

        if self._cancelled:
            for node in graph.nodes:
                if node.status in ("pending", "ready"):
                    node.status = "cancelled"
            await asyncio.to_thread(self._cleanup, keep_failed=False)
            yield {"type": "orchestration_cancelled", "graph": graph.to_json()}
            return

        if any(n.status == "completed" and n.branch for n in graph.nodes):
            yield {"type": "integration_started", "base_branch": base_branch}
            try:
                self.integration = await asyncio.to_thread(
                    integrate, self.repo, graph, self.base_commit
                )
            except GitError as exc:
                yield {"type": "integration_failed", "error": str(exc)}
            else:
                yield {"type": "integration_completed", **self.integration.to_json()}
        kept = await asyncio.to_thread(self._cleanup, keep_failed=True)
        clean = all(n.status == "completed" for n in graph.nodes) and (
            self.integration is None or not self.integration.conflicts
        )
        yield {
            "type": "orchestration_completed",
            "status": "ready_for_verification" if clean else "needs_attention",
            "graph": graph.to_json(),
            "integration": self.integration.to_json() if self.integration else None,
            "kept_worktrees": list(kept),
        }

    # ----- scheduling --------------------------------------------------------

    async def _run_nodes(self) -> AsyncGenerator[JsonObject]:
        while True:
            if not self._cancelled:
                for node_id in self.graph.block_failed():
                    yield {"type": "node_blocked", "node_id": node_id}
                self._schedule()
            if not self._tasks:
                return
            event = await self._queue.get()
            if event.get("type") == "_task_done":
                if (terminal := self._reap(str(event["node_id"]))) is not None:
                    yield terminal
                continue
            yield event

    def _schedule(self) -> None:
        slots = self.options.max_parallel - len(self._tasks)
        for node in self.graph.ready():
            if slots <= 0:
                return
            lease = None
            if node.mutating:
                lease = self._leases.try_acquire(
                    self.graph.task_id,
                    node.id,
                    node.write_scope,
                    ttl_s=self.options.node_timeout_s * 2,
                )
                if lease is None:
                    continue  # an overlapping mutating node is running: serialize
            node.status = "running"
            self._held[node.id] = lease
            task = asyncio.create_task(self._run_node(node))
            task.add_done_callback(
                lambda _t, nid=node.id: self._queue.put_nowait(
                    {"type": "_task_done", "node_id": nid}
                )
            )
            self._tasks[node.id] = task
            slots -= 1

    def _reap(self, node_id: str) -> JsonObject | None:
        """Forget a finished task; report nodes that died without a terminal event."""

        task = self._tasks.pop(node_id)
        if lease := self._held.pop(node_id, None):
            self._leases.release(lease)
        node = self.graph.node(node_id)
        if node.status != "running":
            return None
        if task.cancelled():
            node.status = "cancelled"
            return {"type": "node_cancelled", "node_id": node_id}
        node.status = "failed"
        node.error = f"internal error: {task.exception()!r}"
        return {"type": "node_failed", "node_id": node_id, "error": node.error}

    # ----- one node ----------------------------------------------------------

    def _emit(self, event: JsonObject) -> None:
        self._queue.put_nowait(event)

    async def _run_node(self, node: TaskNode) -> None:
        session: InteractiveClaudeSession | None = None
        try:
            async with asyncio.timeout(self.options.node_timeout_s):
                cwd = await asyncio.to_thread(self._prepare, node)
                mode = self.options.permission_mode if node.mutating else "plan"
                session = await self.sessions.start(
                    cwd=str(cwd), permission_mode=mode, model=self.options.model
                )
                self._live[node.id] = session
                self._emit(
                    {
                        "type": "node_started",
                        "node_id": node.id,
                        "role": node.role,
                        "cwd": str(cwd),
                        "branch": node.branch,
                        "permission_mode": mode,
                        "live_id": session.live_id,
                    }
                )
                result = await self._drive(node, session)
            node.session_id = session.session_id
            await self._close(node.id, interrupt=False)
            self._record_result(node, result)
            if node.mutating and node.worktree:
                await asyncio.to_thread(self._commit, node)
            node.status = "completed"
            self._emit(
                {
                    "type": "node_completed",
                    "node_id": node.id,
                    "summary": node.summary,
                    "cost_usd": node.cost_usd,
                    "turns": node.turns,
                    "session_id": node.session_id,
                    "branch": node.branch,
                    "reverted_out_of_scope": list(node.reverted_out_of_scope),
                }
            )
        except asyncio.CancelledError:
            node.status = "cancelled"
            self._emit({"type": "node_cancelled", "node_id": node.id})
            raise
        except TimeoutError:
            self._fail(
                node, f"wall time budget of {self.options.node_timeout_s}s spent"
            )
        except (ChatSessionError, GitError, NodeFailedError) as exc:
            self._fail(node, str(exc))
        finally:
            if session is not None:
                await self._close(node.id, interrupt=node.status != "completed")

    def _fail(self, node: TaskNode, error: str) -> None:
        node.status = "failed"
        node.error = error
        self._emit(
            {
                "type": "node_failed",
                "node_id": node.id,
                "error": error,
                "worktree": node.worktree,
            }
        )

    def _prepare(self, node: TaskNode) -> Path:
        """Return the node's cwd; mutating nodes get a worktree holding their deps' work."""

        if not node.mutating:
            return self.repo
        worktree = create_worktree(
            self.repo, self.graph.task_id, node.id, self.base_commit
        )
        node.branch, node.worktree = worktree.branch, str(worktree.path)
        ancestors = self.graph.ancestors(node.id)
        for dep_id in self.graph.order():
            dep = self.graph.node(dep_id)
            if dep_id in ancestors and dep.status == "completed" and dep.branch:
                git(worktree.path, "merge", "-q", "--no-edit", dep.branch)
        node.start_commit = git(worktree.path, "rev-parse", "HEAD")
        return worktree.path

    async def _drive(
        self, node: TaskNode, session: InteractiveClaudeSession
    ) -> JsonObject:
        """Send the node prompt and wait for Claude's ``result`` event."""

        events = session.subscribe()
        try:
            await session.send_user_message(self._prompt(node))
            async for event in events:
                kind = event.get("type")
                if kind == "result":
                    return event
                if kind == "fcc_exit":
                    raise ChatSessionError(
                        f"Claude Code exited (code {event.get('code')}): "
                        f"{event.get('stderr') or ''}".strip()
                    )
                if kind == "assistant" and (tools := _tool_names(event)):
                    self._emit(
                        {"type": "node_progress", "node_id": node.id, "tools": tools}
                    )
            raise ChatSessionError("Claude Code ended without a result.")
        finally:
            await events.aclose()

    def _record_result(self, node: TaskNode, result: JsonObject) -> None:
        cost, turns, text = (
            result.get("total_cost_usd"),
            result.get("num_turns"),
            result.get("result"),
        )
        node.cost_usd = float(cost) if isinstance(cost, int | float) else None
        node.turns = turns if isinstance(turns, int) else None
        node.summary = (text if isinstance(text, str) else "")[:_SUMMARY_LIMIT]
        if result.get("subtype") != "success" or result.get("is_error") is True:
            raise NodeFailedError(node.summary or f"result: {result.get('subtype')}")

    def _commit(self, node: TaskNode) -> None:
        assert node.worktree is not None
        path = Path(node.worktree)
        commit_all(path, f"fcc: {node.id}: {node.objective[:60]}")
        node.reverted_out_of_scope += enforce_scope(path, node)

    async def _close(self, node_id: str, *, interrupt: bool) -> None:
        session = self._live.pop(node_id, None)
        if session is None:
            return
        if interrupt and not session.exited:
            with contextlib.suppress(ChatSessionError, TimeoutError):
                await asyncio.wait_for(
                    session.control({"subtype": "interrupt"}),
                    self.options.interrupt_timeout_s,
                )
        if not await self.sessions.close(session.live_id):
            await session.close()

    def _cleanup(self, *, keep_failed: bool) -> list[str]:
        """Remove worktrees (and branches) unless kept for debugging; returns kept paths."""

        merged = set(self.integration.merged if self.integration else [])
        kept: list[str] = []
        for node in self.graph.nodes:
            if not node.worktree:
                continue
            if keep_failed and node.status != "cancelled" and node.id not in merged:
                kept.append(node.worktree)
                continue
            remove_worktree(self.repo, Path(node.worktree), node.branch)
            logger.debug("Removed worktree {}", node.worktree)
        return kept

    # ----- prompt ------------------------------------------------------------

    def _prompt(self, node: TaskNode) -> str:
        opts = self.options
        lines = [
            f"You are the {node.role} agent for node '{node.id}' of a larger task.",
            "",
            "Objective:",
            node.objective,
        ]
        if opts.contract_block:
            lines += [
                "",
                "Intent constraints (binding, verbatim):",
                opts.contract_block,
            ]
        lines.append("")
        if node.mutating:
            lines.append(
                "Write scope: modify ONLY files matching these repo-relative globs: "
                + ", ".join(node.write_scope)
                + ". Changes outside the scope are reverted."
            )
        else:
            lines.append("This node is read-only: do not create or modify files.")
        if node.acceptance_criteria:
            lines += ["", "Acceptance criteria:"]
            lines += [f"- {c}" for c in node.acceptance_criteria]
        deps = [self.graph.node(d) for d in node.depends_on]
        if deps:
            lines += ["", "Results of the nodes this one depends on:"]
            lines += [f"- {d.id}: {d.summary or '(no summary)'}" for d in deps]
        lines += [
            "",
            f"Budget: finish within {opts.max_turns} turns.",
            "Do not commit, push, or switch branches; the orchestrator integrates your work.",
            "Stop and summarize when done: reply with a short summary of what you "
            "changed or found and how the acceptance criteria are met.",
        ]
        return "\n".join(lines)


def _tool_names(event: JsonObject) -> list[str]:
    message = event.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return []
    return [
        str(block["name"])
        for block in content
        if isinstance(block, dict)
        and block.get("type") == "tool_use"
        and isinstance(block.get("name"), str)
    ]
